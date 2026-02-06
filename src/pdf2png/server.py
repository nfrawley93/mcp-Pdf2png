import asyncio
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from pdf2image import convert_from_path
import os
import tempfile
import re
import aiohttp

server = Server("pdf2png")

# Helper: Check if string is a URL
def is_url(path: str) -> bool:
    return re.match(r'^https?://', path) is not None

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
        )
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool execution requests."""
    if name != "pdf2png":
        raise ValueError(f"Unknown tool: {name}")

    if not arguments:
        raise ValueError("Missing arguments")

    read_file_path = arguments.get("read_file_path")
    write_folder_path = arguments.get("write_folder_path")

    if not read_file_path or not write_folder_path:
        raise ValueError("Missing 'read_file_path' or 'write_folder_path'")

    # Determine if we're dealing with a remote URL
    temp_pdf_path = None
    try:
        if is_url(read_file_path):
            print(f"Downloading PDF from {read_file_path}...")
            async with aiohttp.ClientSession() as session:
                async with session.get(read_file_path) as response:
                    response.raise_for_status()
                    # Create temporary file to store downloaded PDF
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                        temp_pdf_path = tmp_pdf.name
                        while True:
                            chunk = await response.content.read(8192)
                            if not chunk:
                                break
                            tmp_pdf.write(chunk)
            read_file_path = temp_pdf_path  # Override with local path

        # Convert PDF to PNG (now either local or downloaded)
        images = convert_from_path(read_file_path)

        # Create output directory if it doesn't exist
        os.makedirs(write_folder_path, exist_ok=True)

        # Save each page as PNG
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
        # Clean up temporary file if we created one
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            try:
                os.unlink(temp_pdf_path)
            except Exception as e:
                print(f"Warning: Failed to delete temp file {temp_pdf_path}: {e}")

async def main():
    # Run the server using stdin/stdout streams
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
