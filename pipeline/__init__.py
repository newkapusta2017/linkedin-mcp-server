"""
LinkedIn saved-posts → calendar pipeline.

Separate from the MCP server. Consumes saved-post data, classifies events
via the Anthropic API, and creates Google Calendar entries.
"""
