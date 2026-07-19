# Monty & CodeMode Review and Optimization Guide

This document reviews how the **Pydantic AI Security Skills Runner** utilizes **Monty** (via the `CodeMode` capability) and identifies optimization opportunities to improve performance, cache efficiency, and compatibility with the analyst skills.

---

## 1. Current Architecture Overview

In [runner.py](../skill_runner/runner.py), the `Agent` is initialized with two capabilities:
1. `FileSystem(root_dir=str(ws_path))`
2. `CodeMode(...)` with workspace mounting:
   ```python
   CodeMode(
       mount=MountDir(SANDBOX_WORKSPACE_MOUNT, str(ws_path), mode="read-write"),
       os_access=OSAccess(environ={}),
   )
   ```

Under this configuration:
* **Host Filesystem Isolation**: The host workspace is mounted into the sandbox at `/workspace`. The sandbox is isolated from other parts of the host filesystem.
* **Environment Variable Isolation**: An empty environment dictionary (`OSAccess(environ={})`) prevents the sandbox from reading host environment variables (like API keys or cloud credentials).
* **Clock Access**: Only the host clock is exposed to allow generated scripts to write timestamped output files.
* **Tool Access**: By default, `CodeMode` wraps **all** of the agent's tools (including the `FileSystem` toolset) inside the sandbox, making them callable only through LLM-generated Python code inside `run_code`.

---

## 2. Inefficiencies & Optimization Opportunities

### 🚀 Optimization 1: Selective Tool Sandboxing (Excluding FileSystem Tools)
* **The Problem**: By default, `CodeMode(tools='all')` wraps all tools inside the sandbox. This hides the `FileSystem` tools (`list_directory`, `read_file`, `write_file`, `grep_search`) from the agent as native top-level tool calls. 
  However, in [SKILL.md](../skills/suricata-analyst/SKILL.md), the instructions explicitly direct the agent to call these natively:
  > *Before writing any analysis code, call `list_directory(path='.')` (the FileSystem tool, not `run_code`) to see what's actually in the workspace...*
  
  Because the tools are sandboxed, the agent cannot call `list_directory` natively. It is forced to run `run_code` and execute Python code just to list files or read a template, which is slow, consumes more tokens, and burns model turns.
* **The Insight**: The Monty sandbox does **not** need the `FileSystem` tools wrapped inside it to access files. Because the host workspace is mounted directly via `MountDir('/workspace', ...)`, the sandboxed Python code can use native `pathlib.Path` to read/write files (e.g. `pathlib.Path('/workspace/eve.json').open()`).
* **The Solution**: Configure `CodeMode` to exclude the `FileSystem` tools:
  ```python
  CodeMode(
      tools=[],  # Leaves FileSystem tools visible as normal native tool calls
      mount=MountDir(SANDBOX_WORKSPACE_MOUNT, str(ws_path), mode="read-write"),
      os_access=OSAccess(environ={}),
  )
  ```
  This restores native tool calling for directory listing and template reading, improving speed and matching the skill guidelines.

---

### ⚡ Optimization 2: Enabling `dynamic_catalog` for Cache Stability
* **The Problem**: In default mode (`dynamic_catalog=False`), `CodeMode` renders the signatures of all sandboxed tools directly into `run_code`'s tool description. If tools are added, modified, or discovered dynamically (e.g., via Tool Search), the description changes, which busts the prefix prompt cache for all subsequent model calls.
* **The Solution**: Set `dynamic_catalog=True` in `CodeMode`:
  ```python
  CodeMode(
      dynamic_catalog=True,
      ...
  )
  ```
  This moves the "available functions" signatures out of the `run_code` description (which stays static and cache-warm) and puts them into a dynamic system instruction part. For static toolsets, the default is fine, but if dynamic workflows or tool discovery are added, this is a critical optimization.

---

### 🛡️ Optimization 3: Processing Performance on Large Logs
* **The Problem**: The input logs (e.g. `eve-2026-01-21-01.json` at **251 MB**) are large. If the model writes code that loads the entire file into memory (such as `json.loads(f.read())`), it can cause Out Of Memory (OOM) errors and hang the interpreter.
* **The Solution**: 
  1. Continue enforcing strict streaming in `SKILL.md` (using `f.readline()` loops).
  2. To make it even more efficient, we could write a optimized, compiled, or helper function in python that streams and filters logs natively, and register it as an agent tool. Since `CodeMode` wraps tools, any helper tool we expose at the agent level will automatically become callable inside the sandbox!
     *Example*: Expose a `filter_log_by_time` tool. The agent can call `await filter_log_by_time(...)` inside its generated script, which runs highly optimized Python on the host side, returning only the filtered/reduced records to the sandbox.

---

## 3. Recommended Code Changes

We can modify the agent instantiation in `runner.py` to:
1. Restore native `FileSystem` tool execution by passing `tools=[]` to `CodeMode`.
2. Add a `--no-sandbox-fs` flag or clean default behavior so `FileSystem` is native.

```diff
     agent = Agent(
         args.model,
         system_prompt=instructions,
         capabilities=[
             FileSystem(root_dir=str(ws_path)),
             CodeMode(
+                tools=[],  # Keep FileSystem tools native to avoid running Monty for directory listings
                 mount=MountDir(SANDBOX_WORKSPACE_MOUNT, str(ws_path), mode="read-write"),
                 # Empty environ keeps host env vars isolated; only the host clock is exposed,
                 # so generated code can timestamp filenames per the skill's naming convention.
                 os_access=OSAccess(environ={}),
             ),
         ],
     )
```

---

## 4. Summary of Benefits

| Feature | Default/Current Setup | Optimized Setup | Business/Perf Impact |
| --- | --- | --- | --- |
| **FileSystem Tools** | Sandboxed (`tools='all'`) | Native (`tools=[]`) | Faster discovery, no Monty VM initialization overhead for directory listing/file reading, matches `SKILL.md`. |
| **File I/O inside Monty** | Supported via `/workspace` mount | Supported via `/workspace` mount | Remains unchanged; sandboxed scripts use standard `pathlib.Path` to process logs. |
| **Tool Descriptions** | Rendered in tool description | Uses dynamic catalog (if enabled) | Significant prompt-cache savings on models that support prefix caching. |
