# Latita agent notes

## Architecture

Latita replaces the previous `ephemctl` bash/python monolith with a clean Python package built on `libvirt`/`qemu`.

### Key modules

- `config.py` — root directory resolution, `.latita` / `.cap` YAML loaders, project-level `.latita` config
- `operations.py` — VM CRUD, ephemeral lifecycle enforcement, revive logic, one-shot `run_instance`
- `cloudinit.py` — cloud-config generation; merges template + capsule fragments
- `capsules.py` — capsule registry, compatibility checks, `depends_on` dependency resolution, live vs create-time application
- `hardening.py` — SELinux, no-guest-agent, nwfilter egress controls
- `libvirt.py` — thin wrapper around `virsh` and `virt-install`
- `metadata.py` — JSON-based instance state (recipe, spec, env)
- `cli.py` — Typer CLI with subcommands for VMs, capsules, templates, and the `run` one-shot runner
- `tui.py` — Textual TUI dashboard (two-pane, keyboard-driven) for VM list, actions, templates, and capsules
- `prompts.py` — tiered interactive wizards (simple, advanced, full) and template generator

### File formats

Templates live in `<root>/templates/*.latita` as YAML.
Capsules live in `<root>/capsules/*.cap` as YAML.
Project-level config lives in `.latita` (cwd) as YAML.

### Ephemeral lifecycle

Stored in `spec.json` per instance:
- `transient` — passed to virt-install
- `destroy_on_stop` — host-side shred + rm on `latita stop`
- `max_runs` / `run_count` — checked on `start`
- `expire_at` — absolute ISO timestamp checked on `start`

### Security defaults

- `selinux: true` and `no_guest_agent: true` are defaults.
- `restrict_network` applies a libvirt nwfilter; `allow_hosts` can narrow it.
- Disk overlays are shredded with `shred -n 3` on destroy.
- **Networking is isolated by default** (`mode: isolated`). VMs have no external access unless `--net` or `network: nat` is explicitly set.

### UX design principles

- **Defaults over configuration**: templates encode decisions; the CLI is for exceptions.
- **Secure by default**: network off, SELinux on, no guest agent. Explicit opt-in for connectivity.
- **Tiered interaction**: `create` is 2 prompts, `--advanced` adds resources, `--full` exposes everything.
- **One-shot runner**: `latita run` for ephemeral, auto-cleaned VMs with no persistent state.
- **Project config**: `.latita` file in cwd merged with CLI flags, similar to a `Smolfile`.

### System vs Session mode

| | `qemu:///system` | `qemu:///session` |
|---|---|---|
| **Privileges** | Root / sudo required for network setup | Unprivileged — works out of the box |
| **Networking** | Shared NAT bridges (e.g. `default` at 192.168.122.0/24). VMs on the same network can talk to each other. | Isolated SLIRP per VM (10.0.2.0/24). VMs **cannot** reach each other. |
| **SSH access** | Direct VM IP (e.g. 192.168.122.235) | `localhost:PORT` via QEMU `hostfwd` (port 2222-9999) |
| **Use case** | Production, multi-VM labs, networking tests | Quick one-offs, CI, unprivileged workstations |
| **Test suite** | `test_desktop_minimal_reaches_code_server` (MVP E2E) requires system mode | All other E2E tests run in session mode |

**Session mode specifics**: When `LIBVIRT_DEFAULT_URI` is `qemu:///session` (or auto-detected as fallback), latita:
- Skips root-only network setup (no bridge creation)
- Forces `user` networking (SLIRP) even if the template asks for `nat`
- Skips `setfacl` / `grant_qemu_path_access`
- Injects `--qemu-commandline` with `hostfwd=tcp::PORT-:22` so SSH works via localhost
- The forwarded port is stored in instance env as `FORWARDED_SSH_PORT`

**System mode specifics**:
- Requires `libvirtd` running and the user in the `libvirt` group (or sudo)
- Creates/activates the `default` NAT network for shared VM-to-VM routing
- Uses the VM's actual DHCP-assigned IP for SSH
- **Multi-VM E2E tests** (desktop → headless code-server) only work in system mode because session-mode VMs live on isolated SLIRP networks

**Auto-detection**: `Config.default()` probes `qemu:///system` at startup. If the socket is absent or connection is refused, it automatically falls back to `qemu:///session`. Set `LIBVIRT_DEFAULT_URI` explicitly to override.

**Libvirt connectivity check**: Before any VM operation, latita verifies the configured libvirt URI is reachable. If `qemu:///system` is unavailable, `create_instance` fails with: `Cannot connect to libvirt at qemu:///system. Set LIBVIRT_DEFAULT_URI=qemu:///session to use user-level libvirt without sudo, or ensure the system libvirtd daemon is running and sudo is configured.`

**virt-install and system gi**: `virt-install` depends on `gi` (PyGObject) which is typically installed at the system Python level. When latita runs under `uv run`, the venv's site-packages takes precedence over system site-packages, so `virt-install` would fail to find `gi`. To fix this, `virt_install()` in `libvirt.py` detects the system site-packages path (by probing `/usr/bin/python3`) and injects it via `PYTHONPATH` when calling virt-install. This ensures virt-install works correctly regardless of whether it's invoked via `uv run` or directly.

### Why Python (not Rust)

Latita is a CLI orchestrator, not a VMM. The actual runtime is spent waiting on:
- `virt-install` (5-30s)
- `qemu-img` operations (1-5s)
- VM boot (2-60s)
- SSH round-trips

Python adds ~100ms startup overhead to commands that take 15-60s. A Rust rewrite would cut that to ~15ms — a 0.1% improvement at the cost of ~2-4 weeks of rewrite work, new dependency chains (libvirt bindings, SSH clients), and rewriting 124 tests.

Rewrite to Rust would make sense if latita were:
- A micro-VMM (like Firecracker) needing sub-millisecond boots
- A daemon handling thousands of requests/second
- Deployed to an embedded environment without a Python runtime

For a CLI that orchestrates libvirt/QEMU, Python is the right trade-off.

### Capsule dependency system

- `depends_on: [capsule-name]` declares dependencies.
- `resolve_capsules()` does depth-first traversal, deduplicates, and orders so dependencies provision before dependents. Returns `(name, data)` tuples.
- Cycle detection raises a clear error.
- `code-server` depends on `podman-host`; `open-webui` depends on `ollama`.
- The `ai-agents` capsule installs Claude Code, Codex, Gemini CLI, Kimi CLI, OpenCode, and Pi.
- `apply_capsule_live()` automatically resolves and applies dependencies in order before the requested capsule.

### Live apply self-containment

Capsule `live.commands` should be idempotent and self-contained — they may run on bare VMs that were created without the capsule's `provision.packages`. Both `ai-agents` and `code-server` include conditional `dnf`/`apt-get` installation blocks in their live commands so missing dependencies are installed on-the-fly.

### Verified capsules (end-to-end)

The following capsules have been verified on live Fedora 43 VMs (session mode):

| Capsule | Install path | Verified |
|---|---|---|
| `ai-agents` | `npm install -g` to `~/.local` + `pip3 --user` | claude 2.1.123, codex 0.125.0, gemini 0.40.0, opencode 1.14.30, pi 0.70.6, kimi 1.41.0 |
| `code-server` | `podman run ghcr.io/coder/code-server:latest` | Container running, HTTP 200 on `:8443/login` |
| `podman-host` | `dnf install -y podman crun slirp4netns fuse-overlayfs container-selinux` | podman 5.6.2 |

### Tests

Run `python -m py_compile src/latita/*.py` for a quick smoke check.
Run `.venv3/bin/python -m pytest tests/` for the full suite (195 tests including real VM lifecycle, capsule dependency resolution, cloud-init provision merging, end-to-end SSH to a live Fedora VM, and heavy capsule integration tests).

### Known bugs fixed

- **dnf package block `+` artifact**: `_package_install_block` in `cloudinit.py` previously joined package names with ` \\n+      `, causing literal `+` arguments to be passed to `dnf install`. This caused all multi-package installs to fail with "No match for argument: +". Fixed by removing the stray `+` characters from the join string.

### Base image catalog maintenance

`BASE_IMAGES` in `config.py` stores the curated list of downloadable base images. **Fedora entries use directory URLs with `discover: True`** — the latest point release is scraped from the directory listing at download time, so point-release churn (e.g., `1.6` → `1.7`) never breaks downloads or tests.

When a **new Fedora major release** drops (e.g., Fedora 44):
1. Verify Cloud images exist at `releases/N/Cloud/x86_64/images/` (not just the release root).
2. Add the entry to `BASE_IMAGES` with `discover: True` and a directory URL.
3. Update `Config.default()`'s `default_base_name` if adopting the new release as default.
4. Do **not** auto-detect the latest major release at runtime — new releases may lack Cloud images for weeks, or the image format may change.

Ubuntu LTS entries (e.g., `noble/current/`) use a stable `current/` symlink and do not need discovery.

**Mirror fallbacks**: Fedora entries include `mirror_urls` (e.g., `mirrors.kernel.org`). `_download_base` tries the primary redirector first, then falls back to mirrors if the connection fails (e.g., FCIX mirror `edgeuno-bod2.mm.fcix.net` returning 443 errors). Each attempt uses `curl --retry 3 --connect-timeout 30 --max-time 600` for resilience.

### Desktop template authoring (lessons learned)

Creating a working graphical desktop VM template is deceptively fragile. The following was verified end-to-end on Fedora 43 in session mode (qemu:///session, VGA video, SLIRP networking).

#### The working autologin-to-X pattern

For a minimal desktop (Openbox, no display manager):

1. **getty autologin override** — write a systemd drop-in for `getty@tty1.service`:
   ```
   [Service]
   ExecStart=
   ExecStart=-/usr/sbin/agetty --autologin {guest_user} --noclear %I $TERM
   ```
   This is the *only* reliable way to get a TTY login without a password prompt. Do NOT write a custom `Type=simple` systemd service that calls `startx` — it will fail because `startx` needs a controlling terminal.

2. **User shell startup triggers X** — write `~/.bash_profile` (not `.bashrc`):
   ```bash
   if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
       exec startx
   fi
   ```
   Using `exec` replaces the shell so there's no stray bash process.

3. **`~/.xinitrc` runs the window manager** — this is what `startx` executes:
   ```bash
   #!/bin/bash
   lxpanel &
   exec openbox-session
   ```

4. **Restart getty after the override is written** — cloud-init writes the drop-in, but `getty@tty1` was already started with the old config. Add `systemctl restart getty@tty1` to `root_commands` or the user will still see a password prompt.

5. **`graphical.target` must be the default** — `systemctl set-default graphical.target` ensures getty starts at boot. Without this, the VM may hang in `multi-user.target` with no TTY.

#### What will break if you do it differently

| Bad approach | Why it fails |
|---|---|
| Custom `autologin-startx.service` with `Type=simple` | `startx` needs a controlling TTY; `Type=simple` services have none. It exits immediately. |
| Custom service + `Wants=getty@tty1` | getty claims tty1 first; `startx` tries to grab it, fails, and the service restarts in a loop. SPICE shows the text login prompt. |
| Writing `~/.bashrc` instead of `~/.bash_profile` | `bashrc` is for interactive shells; agetty spawns a login shell which sources `~/.bash_profile` (or `~/.profile`). |
| No `systemctl restart getty@tty1` after writing the drop-in | The already-running getty keeps using the old config. The user still sees a manual login prompt. |
| Hardcoded `/etc/X11/xorg.conf.d/10-video.conf` with `Driver "qxl"` | If the VM is launched with `--video vga` (session-mode fallback), Xorg crashes because the qxl driver can't drive a VGA device. Always let Xorg auto-detect. |
| Missing `xorg-x11-drv-qxl` (or distro equivalent) | Xorg auto-detection finds the QXL device but can't load a driver for it. Xorg crashes. SPICE falls back to text console. |
| Missing `xorg-x11-xinit` | No `startx` command. Nothing launches X. |
| Missing `spice-vdagent` | Clipboard and display resize won't work in SPICE. Not fatal, but annoying. |
| `systemctl enable spice-vdagentd` on Fedora 43 | The package installs `spice-vdagent.service`, not `spice-vdagentd.service`. The enable fails silently because of `|| true`, but don't add it in the first place. |
| Using `tint2` on Fedora 43 | Package does not exist in repos. dnf treats a missing package as fatal, so the *entire* install fails — no Xorg, no openbox, nothing. Always verify packages exist in the target release. |

#### Package checklist for Fedora minimal desktop

```
- xorg-x11-server-Xorg       # X server
- xorg-x11-xinit             # startx
- xorg-x11-drv-qxl           # MUST match virt-install --video model (qxl or vga)
- mesa-dri-drivers           # software/GL fallback
- openbox                    # window manager
- lxpanel                    # taskbar (tint2 not available in Fedora 43)
- xterm                      # terminal
- spice-vdagent              # clipboard/resize
- firefox                    # optional
```

#### For display-manager desktops (e.g. XFCE + lightdm)

Use a display manager if you want a greeter, session selection, or lock screen:

```
- lightdm
- lightdm-gtk
- xfce4-session
- xfce4-panel
- xfdesktop
- Thunar
```

Write `/etc/lightdm/lightdm.conf.d/50-autologin.conf`:
```
[Seat:*]
autologin-user={guest_user}
autologin-user-timeout=0
user-session=xfce
greeter-session=lightdm-gtk-greeter
```

Enable lightdm: `systemctl enable lightdm && systemctl set-default graphical.target`

No xorg.conf needed — Xorg auto-detects the driver. The same video driver package (`xorg-x11-drv-qxl`) is still required because virt-install defaults to `--video qxl` for desktop profiles.

#### Porting to other distros

| Fedora | Debian/Ubuntu | Alpine |
|---|---|---|
| `xorg-x11-server-Xorg` | `xserver-xorg` | `xorg-server` |
| `xorg-x11-xinit` | `xinit` | `xinit` |
| `xorg-x11-drv-qxl` | `xserver-xorg-video-qxl` | `xf86-video-qxl` |
| `openbox` | `openbox` | `openbox` |
| `lxpanel` | `lxpanel` | `lxpanel` |
| `spice-vdagent` | `spice-vdagent` | `spice-vdagent` |
| `systemctl restart getty@tty1` | Same | `rc-service agetty.tty1 restart` |
| getty drop-in path | Same | `/etc/conf.d` style |

The autologin mechanism differs by init system, but the principle is the same: **let the existing getty service auto-login the user, then have the user's shell startup run `startx`.**

### Future work

- **Snapshot / clone support**: `latita snapshot <name>` and `latita clone <name> <new-name>` using `qemu-img` backing chains.
- **Template marketplace**: Share templates via a Git-based registry or simple HTTP index.
