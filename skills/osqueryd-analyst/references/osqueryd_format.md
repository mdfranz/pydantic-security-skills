# Osqueryd Results File Format Reference

## Format Overview
- **Line-based JSON (JSONL)**: Each line is a complete JSON object representing one log record from an osqueryd query result.
- **Common Metadata Fields**:
  - `name`: The name of the query that generated this result (e.g., `all_processes`).
  - `hostIdentifier`: The hostname or identifier of the system.
  - `calendarTime`: UTC timestamp in human-readable format.
  - `unixTime`: Epoch timestamp.
  - `action`: The differential state change (`added` or `removed`).
  - `decorations`: Contextual tags added to the event (e.g., `host_uuid`, `username`).
  - `columns`: Query-specific data columns containing the system telemetry.

## Logged Queries & Target Columns

### 1. `active_processes`
Used to monitor processes that are active or listening.
- **Key Columns**:
  - `name`: Process name (e.g., `dockerd`, `systemd`, `Suricata-Main`).
  - `path`: Absolute binary path.
  - `pid`: Process ID.
  - `port`: Port number (if listening/socket).
  - `address`: IP address (if bound to a socket).

### 2. `all_processes`
Comprehensive snapshots/deltas of all running processes.
- **Key Columns**:
  - `pid`, `parent`: Process ID and Parent Process ID (essential for process tree reconstruction).
  - `name`, `path`: Process name and path.
  - `cmdline`: Command line arguments (highly useful for detecting obfuscation, malicious flags, or parameters).
  - `cwd`: Current working directory.
  - `uid`, `gid`, `euid`, `egid`: User and Group context (elevated privileges check).
  - `start_time`: Time process started.
  - `state`: Process execution state.

### 3. `net_processes`
Telemetry for active network connections initiated by processes.
- **Key Columns**:
  - `pid`, `name`: Owner process details.
  - `local_address`, `local_port`: Source endpoint.
  - `remote_address`, `remote_port`: Destination endpoint (useful for hunting external C2, scanning, or data egress).
  - `state`: Connection state (e.g., `ESTABLISHED`).

### 4. `shell_history`
History of interactive shell commands run on the system.
- **Key Columns**:
  - `command`: The command string executed.
  - `history_file`: Source history file (e.g., `/root/.bash_history`).
  - `uid`: User ID executing the command.

### 5. `suspicious_process_envs`
Environment variables matching potential risk conditions.
- **Key Columns**:
  - `name`, `path`, `pid`: Process details.
  - `key`: Name of the environment variable (e.g., `LD_LIBRARY_PATH`, `LD_PRELOAD`).
  - `value`: Content of the environment variable.

### 6. `installed_packages`
Packages/software installed on the host.
- **Key Columns**:
  - `name`: Package name.
  - `version`: Version string.
  - `source`: Source repository/package manager.

### 7. `system_info`
General system inventory details.
- **Key Columns**:
  - `hostname`: Machine name.
  - `cpu_brand`: CPU description.
  - `physical_memory`: Installed RAM.

## Threat Hunting Pivots
- **Process Lineage / Tree**: Trace parent-child pid relationships in `all_processes` (e.g., parent `webserver` spawning `sh`/`bash`).
- **Network Connections by Process**: Link `net_processes` (`pid`) to `all_processes` (`pid`) to see what command lines and paths are initiating external connections.
- **Unusual Environment Variables**: Watch for environment manipulation in `suspicious_process_envs` (e.g., hijacking library paths).
- **Execution Patterns**: Look for command line patterns in `shell_history` and `all_processes.cmdline` targeting sensitive files (e.g., `/etc/passwd`, `/etc/shadow`) or fetching payloads.
