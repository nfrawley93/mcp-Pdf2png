import asyncio
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from pdf2image import convert_from_path
import os
import tempfile
import re
from urllib.request import urlopen, Request
from concurrent.futures import ThreadPoolExecutor
import json
from base64 import b64encode

server = Server("pdf2png")

# Helper: Check if string is a URL
def is_url(path: str) -> bool:
    return re.match(r'^https?://', path) is not None


# Synchronous helper: download file from URL to local path
def download_file(url: str, filepath: str) -> None:
    with urlopen(url) as response:
        with open(filepath, 'wb') as f:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                f.write(chunk)


# Synchronous helper: POST file to URL using multipart/form-data + optional Basic Auth
def post_file(url: str, filepath: str, username: str | None = None, password: str | None = None) -> None:
    from mimetypes import guess_type

    # Read file content
    with open(filepath, 'rb') as f:
        file_data = f.read()

    # Prepare multipart form data
    boundary = b'------------------------' + str(hash(url)).encode()
    headers = {
        'Content-Type': f'multipart/form-data; boundary={boundary.decode()}'
    }

    # Add Basic Auth if credentials provided
    if username and password:
        credentials = f"{username}:{password}".encode()
        auth_header = b"Basic " + b64encode(credentials)
        headers['Authorization'] = auth_header

    # Build body
    body_parts = []
    filename = os.path.basename(filepath)
    mime_type = guess_type(filename)[0] or 'application/octet-stream'

    body_parts.extend([
        b'--' + boundary,
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
        f'Content-Type: {mime_type}'.encode(),
        b'',
        file_data
    ])

    # Final boundary
    body_parts.extend([b'--' + boundary + b'--', b''])

    body = b'\r\n'.join(body_parts)

    # Send request
    req = Request(url, data=body, headers=headers)
    with urlopen(req) as response:
        if response.status >= 400:
            raise Exception(f"HTTP {response.status}: {response.read().decode()}")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="pdf2png",
            description="Converts PDFs to images in PNG format. Accepts local file paths or remote URLs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "read_file_path": {"type": "string"},
                    "write_folder_path": {"type": "string"},
                },
                "required": ["read_file_path", "write_folder_path"],
            },
        ),
        types.Tool(
            name="pdf2png_upload",
            description="Converts PDF to PNG images, uploads them via POST to a URL with optional Basic Auth, then deletes local files. Accepts local paths or URLs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "read_file_path": {"type": "string"},
                    "upload_url": {"type": "string", "format": "uri"},
                    "write_folder_path": {"type": "string"},
                    "auth_username": {"type": "string", "nullable": True},
                    "auth_password": {"type": "string", "nullable": True},
                },
                "required": ["read_file_path", "upload_url", "write_folder_path"],
            },
        ),
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool execution requests."""
    if not arguments:
        raise ValueError("Missing arguments")

    if name == "pdf2png":
        return await _convert_pdf_only(arguments)

    elif name == "pdf2png_upload":
        return await _convert_and_upload_pdf(arguments)

    else:
        raise ValueError(f"Unknown tool: {name}")


async def _convert_pdf_only(arguments: dict) -> list[types.TextContent]:
    """Internal helper: Convert PDF to PNGs (no upload)"""
    read_file_path = arguments.get("read_file_path")
    write_folder_path = arguments.get("write_folder_path")

    if not read_file_path or not write_folder_path:
        raise ValueError("Missing 'read_file_path' or 'write_folder_path'")

    temp_pdf_path = None
    try:
        if is_url(read_file_path):
            loop = asyncio.get_event_loop()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                temp_pdf_path = tmp_pdf.name
                await loop.run_in_executor(
                    ThreadPoolExecutor(max_workers=1),
                    download_file,
                    read_file_path,
                    temp_pdf_path
                )
            read_file_path = temp_pdf_path

        images = convert_from_path(read_file_path)
        os.makedirs(write_folder_path, exist_ok=True)

        output_files = []
        for i, image in enumerate(images):
            output_path = os.path.join(write_folder_path, f'page_{i+1}.png')
            image.save(output_path, 'PNG')
            output_files.append(output_path)

        return [
            types.TextContent(
                type="text",
                text=f"Successfully converted PDF to {len(output_files)} PNG files in {write_folder_path}"
            )
        ]

    finally:
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            try:
                os.unlink(temp_pdf_path)
            except Exception as e:
                print(f"Warning: Failed to delete temp file {temp_pdf_path}: {e}")


async def _convert_and_upload_pdf(arguments: dict) -> list[types.TextContent]:
    """Internal helper: Convert PDF, upload PNGs, then delete locally with optional Basic Auth"""
    read_file_path = arguments.get("read_file_path")
    upload_url = arguments.get("upload_url")
    write_folder_path = arguments.get("write_folder_path")
    auth_username = arguments.get("auth_username")  # Optional
    auth_password = arguments.get("auth_password")  # Optional

    if not read_file_path or not upload_url or not write_folder_path:
        raise ValueError("Missing required fields: 'read_file_path', 'upload_url', or 'write_folder_path'")

    temp_pdf_path = None
    created_pngs = []

    try:
        # Step 1: Download PDF if URL
        if is_url(read_file_path):
            loop = asyncio.get_event_loop()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                temp_pdf_path = tmp_pdf.name
                await loop.run_in_executor(
                    ThreadPoolExecutor(max_workers=1),
                    download_file,
                    read_file_path,
                    temp_pdf_path
                )
            read_file_path = temp_pdf_path

        # Step 2: Convert PDF to PNGs
        images = convert_from_path(read_file_path)
        os.makedirs(write_folder_path, exist_ok=True)

        for i, image in enumerate(images):
            output_path = os.path.join(write_folder_path, f'page_{i+1}.png')
            image.save(output_path, 'PNG')
            created_pngs.append(output_path)

        if not created_pngs:
            raise ValueError("No pages were generated from PDF")

        # Step 3: Upload each PNG with optional Basic Auth
        uploaded_count = 0
        loop = asyncio.get_event_loop()

        for png_path in created_pngs:
            try:
                await loop.run_in_executor(
                    ThreadPoolExecutor(max_workers=1),
                    post_file,
                    upload_url,
                    png_path,
                    auth_username,
                    auth_password
                )
                uploaded_count += 1
                print(f"Uploaded: {png_path}")
            except Exception as e:
                print(f"Failed to upload {png_path}: {e}")

        # Step 4: Delete all local PNGs after upload
        for png_path in created_pngs:
            try:
                os.unlink(png_path)
            except Exception as e:
                print(f"Warning: Failed to delete local file {png_path}: {e}")

        auth_str = f" with Basic Auth" if auth_username else " without authentication"
        return [
            types.TextContent(
                type="text",
                text=f"Successfully converted PDF to {len(created_pngs)} PNG files, uploaded {uploaded_count} of them to {upload_url}{auth_str}, and deleted local copies."
            )
        ]

    finally:
        # Clean up temporary PDF if it was downloaded
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            try:
                os.unlink(temp_pdf_path)
            except Exception as e:
                print(f"Warning: Failed to delete temp PDF file {temp_pdf_path}: {e}")


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="pdf2png",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())
