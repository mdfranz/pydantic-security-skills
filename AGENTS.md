# Generic Guidance
- When I say review, that literally means review code and DO NOT TEST or attempt to BUILD.

# Search Preferences
- Always search local filesystem for Python, Rust, or Golang code before searching Github

# Core Documentation artifacts
Keep the following up to date if they exist and there should be minimum overlap
- ARCHITECTURE.md - structure and data flows - not how to use, often uses Mermaid diagrams
- README.md - high level documentation with links to other docs
- PROJECT.md - chronology of commits and development phases
- THREAT_MODEL.md - adversary model, asset/boundary traceability, and open security risk items - not how components work, that's ARCHITECTURE.md

Only update if they exist.

# Markdown Coventions
- Use relative links
- Never use `file:///`

# Python Python Conventions
- Use `uv` to install packages and manage virtual environments.
- Use `uv` to run scripts


# Skills
- Check for pydantic skills and try to use first, but then look for source code in `.venv` prior to searching the web
