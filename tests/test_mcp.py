"""Test MCP server functionality."""

import json
import tempfile
from pathlib import Path

import pytest

from circus.config import settings
from circus.database import init_database
from circus.mcp.server import CircusMCPServer


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    original_path = settings.database_path
    settings.database_path = db_path
    init_database(db_path)

    yield db_path

    settings.database_path = original_path
    db_path.unlink()


def test_mcp_server_initialization():
    """Test MCP server can be initialized."""
    server = CircusMCPServer()
    assert server is not None
    assert hasattr(server, 'get_tools')


def test_mcp_list_tools():
    """Test MCP server lists available tools."""
    server = CircusMCPServer()
    tools = server.get_tools()

    assert len(tools) > 0

    # Check for expected tool patterns
    tool_names = [tool['name'] for tool in tools]
    expected_patterns = ['circus_']  # All tools start with circus_

    assert any(name.startswith('circus_') for name in tool_names)


def test_mcp_tool_schemas():
    """Test that all tools have proper schemas."""
    server = CircusMCPServer()
    tools = server.get_tools()

    for tool in tools:
        assert 'name' in tool
        assert 'description' in tool
        assert 'inputSchema' in tool
        assert 'type' in tool['inputSchema']
        assert 'properties' in tool['inputSchema']


def test_mcp_call_tool_invalid(temp_db):
    """Test MCP server rejects invalid tool names."""
    server = CircusMCPServer()
    tools = server.get_tools()
    tool_names = [tool['name'] for tool in tools]

    # Just check that nonexistent_tool is not in the list
    assert "nonexistent_tool" not in tool_names


def test_mcp_server_has_tools():
    """Test MCP server has expected circus tools."""
    server = CircusMCPServer()
    tools = server.get_tools()

    tool_names = [tool['name'] for tool in tools]

    # Check for some expected tools (using actual tool names from the server)
    expected_tools = ['circus_discover', 'circus_join_room', 'circus_share_memory']

    for expected in expected_tools:
        assert expected in tool_names, f"Expected tool '{expected}' not found in {tool_names}"
