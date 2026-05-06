from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer

from . import capsules as caps_mod
from .config import get_config, list_capsules, list_latita_templates, load_latita_template, load_project_config, clear_project_config, load_root_config, clear_root_config, write_yaml
from .operations import (
    apply_capsule_live,
    bootstrap_host,
    connect_instance,
    create_instance,
    destroy_instance,
    doctor,
    get_vm_init_state,
    init_base,
    list_instances,
    revive_instance,
    run_instance,
    scan_instances,
    ssh_instance,
    start_instance,
    stop_instance,
    wait_for_vm_ready,
)
from .prompts import (
    interactive_create_simple,
    interactive_create_advanced,
    interactive_create_full,
    interactive_generate_template,
    _pick_vm,
    _pick_running_vm,
    _pick_stopped_vm,
    _pick_capsule,
    prompt_download_base_image,
)
from .ui import console

app = typer.Typer(
    help='Latita - Ephemeral libvirt/QEMU lab manager with capsules',
    invoke_without_command=True,
)

dashboard_app = typer.Typer(help='TUI dashboard for VM management')
app.add_typer(dashboard_app, name='dashboard', rich_help_panel='Interactive')

# Sub-typer for capsules
capsule_app = typer.Typer(help='Manage capsules', invoke_without_command=True)
app.add_typer(capsule_app, name='capsule', rich_help_panel='Management')

# Sub-typer for templates
template_app = typer.Typer(help='Manage templates', invoke_without_command=True)
app.add_typer(template_app, name='template', rich_help_panel='Management')

# Sub-typer for config
config_app = typer.Typer(help='Manage latita configuration')
app.add_typer(config_app, name='config', rich_help_panel='Management')


@app.callback(invoke_without_command=True)
def callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        console.print('[bold]Latita[/bold] - Ephemeral libvirt/QEMU lab manager')
        console.print('Run [dim]latita --help[/dim] for commands or [dim]latita menu[/dim] for interactive mode.')
        raise typer.Exit()


@capsule_app.callback()
def capsule_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        console.print('[bold]Capsule commands:[/bold]')
        console.print('  list  - List available capsules')
        console.print('  apply - Apply a capsule to a running VM (via SSH)')
        console.print()
        console.print('Run [dim]latita capsule <command> --help[/dim] for more info.')


@template_app.callback()
def template_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        console.print('[bold]Template commands:[/bold]')
        console.print('  list     - List available .latita templates')
        console.print('  show     - Show a template (full YAML)')
        console.print('  generate - Interactively generate a new template')
        console.print()
        console.print('Run [dim]latita template <command> --help[/dim] for more info.')


def _ensure_running(name: str) -> None:
    """Print disclaimer and start a VM if it is not already running."""
    entries = {e["name"]: e for e in scan_instances()}
    if entries.get(name, {}).get("status") != "running":
        console.print(
            "[yellow]Sending a command to a stopped VM will start it immediately.[/yellow]"
        )
        start_instance(name)


def _menu_start() -> None:
    name = _pick_stopped_vm("Select VM to start")
    if name:
        start_instance(name)


def _menu_stop() -> None:
    name = _pick_running_vm("Select VM to stop")
    if name:
        stop_instance(name)


def _menu_destroy() -> None:
    name = _pick_vm("Select VM to destroy")
    if name and typer.confirm(f"Destroy VM '{name}' and shred its disk?", default=False):
        destroy_instance(name)


def _menu_ssh() -> None:
    name = _pick_running_vm("Select VM to SSH into")
    if name is None:
        name = _pick_vm("Select VM (stopped VMs will be started first)")
    if name:
        _ensure_running(name)
        ssh_instance(name)


def _menu_connect() -> None:
    name = _pick_running_vm("Select VM to connect")
    if name is None:
        name = _pick_vm("Select VM (stopped VMs will be started first)")
    if name:
        _ensure_running(name)
        connect_instance(name)


def _menu_capsule_apply() -> None:
    name = _pick_running_vm("Select VM for capsule")
    if name is None:
        name = _pick_vm("Select VM (stopped VMs will be started first)")
    if name:
        _ensure_running(name)
        capsule = _pick_capsule("Select capsule")
        if capsule:
            apply_capsule_live(name, capsule)


@app.command(name="menu", rich_help_panel="Interactive")
def menu_cmd() -> None:
        '''Launch the TUI dashboard (live VM monitoring, keyboard shortcuts).'''
        from .tui import Dashboard
        app_instance = Dashboard()
        app_instance.run()


def _build_overrides(
    cpus: Optional[int] = None,
    memory: Optional[int] = None,
    disk: Optional[str] = None,
    net: bool = False,
    allow_host: list[str] | None = None,
    transient: bool = False,
    destroy_on_stop: bool = False,
    max_runs: Optional[int] = None,
    expires: Optional[int] = None,
    no_guest_agent: bool = False,
    no_selinux: bool = False,
    restrict_network: bool = False,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if cpus is not None:
        overrides["cpus"] = cpus
    if memory is not None:
        overrides["memory"] = memory
    if disk is not None:
        overrides["disk_size"] = disk
    if net:
        overrides.setdefault("network", {})["mode"] = "nat"
        overrides.setdefault("network", {})["nat_network"] = "default"
    if allow_host:
        overrides.setdefault("security", {})["restrict_network"] = True
        overrides.setdefault("security", {})["allow_hosts"] = list(allow_host)
    if transient:
        overrides.setdefault("ephemeral", {})["transient"] = True
    if destroy_on_stop:
        overrides.setdefault("ephemeral", {})["destroy_on_stop"] = True
    if max_runs is not None:
        overrides.setdefault("ephemeral", {})["max_runs"] = max_runs
    if expires is not None:
        overrides.setdefault("ephemeral", {})["expires_after_hours"] = expires
    if no_guest_agent:
        overrides.setdefault("security", {})["no_guest_agent"] = False
    if no_selinux:
        overrides.setdefault("security", {})["selinux"] = False
    if restrict_network:
        overrides.setdefault("security", {})["restrict_network"] = True
    return overrides


def _interactive_create(level: str = "simple") -> None:
    try:
        if level == "full":
            recipe = interactive_create_full()
        elif level == "advanced":
            recipe = interactive_create_advanced()
        else:
            recipe = interactive_create_simple()
    except typer.Abort:
        console.print("Aborted.", style="yellow")
        return
    template_name = recipe.get("template_name", recipe["profile"])
    if template_name not in list_latita_templates():
        console.print(f"Template '{template_name}' not found. Creating with defaults...", style="yellow")
    try:
        create_instance(
            template_name=template_name,
            name=recipe.get("name"),
            capsule_names=recipe.get("capsules", []),
            overrides=recipe,
            wait=False,
        )
    except typer.BadParameter as exc:
        if "base image not found" in str(exc):
            if prompt_download_base_image():
                create_instance(
                    template_name=template_name,
                    name=recipe.get("name"),
                    capsule_names=recipe.get("capsules", []),
                    overrides=recipe,
                    wait=False,
                )
        else:
            raise


def _interactive_run() -> None:
    try:
        recipe = interactive_create_simple()
    except typer.Abort:
        console.print("Aborted.", style="yellow")
        return
    template_name = recipe.get("template_name", recipe["profile"])
    try:
        run_instance(
            template_name=template_name,
            overrides=recipe,
            capsule_names=recipe.get("capsules", []),
        )
    except typer.BadParameter as exc:
        if "base image not found" in str(exc):
            if prompt_download_base_image():
                run_instance(
                    template_name=template_name,
                    overrides=recipe,
                    capsule_names=recipe.get("capsules", []),
                )
        else:
            raise


@app.command(name="create", rich_help_panel="VM Lifecycle")
def create_cmd(
    template: str = typer.Argument(..., help="Template name (e.g. headless, desktop)"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="VM name"),
    capsule: list[str] = typer.Option([], "--capsule", "-c", help="Capsule to apply (repeatable)"),
    cpus: Optional[int] = typer.Option(None, "--cpus", help="Override vCPUs"),
    memory: Optional[int] = typer.Option(None, "--memory", "-m", help="Override memory (MiB)"),
    disk: Optional[str] = typer.Option(None, "--disk", "-d", help="Override disk size (e.g. 20G)"),
    net: bool = typer.Option(False, "--net", help="Enable NAT networking"),
    allow_host: list[str] = typer.Option([], "--allow-host", help="Allowed egress host (repeatable)"),
    ephemeral: bool = typer.Option(False, "--ephemeral", "-e", help="Destroy on stop"),
    transient: bool = typer.Option(False, "--transient", "-t", help="Libvirt transient"),
    max_runs: Optional[int] = typer.Option(None, "--max-runs", help="Max start count"),
    expires: Optional[int] = typer.Option(None, "--expires", help="Expire after N hours"),
    no_guest_agent: bool = typer.Option(False, "--no-guest-agent", help="Disable qemu-guest-agent"),
    no_selinux: bool = typer.Option(False, "--no-selinux", help="Disable SELinux hardening"),
    restrict_network: bool = typer.Option(False, "--restrict-network", help="Restrict outbound network"),
    advanced: bool = typer.Option(False, "--advanced", help="Interactive advanced mode"),
    full: bool = typer.Option(False, "--full", help="Interactive full wizard mode"),
    no_wait: bool = typer.Option(False, "--no-wait", help="Return immediately without waiting for initialization"),
) -> None:
    '''Create a VM from a template.

    Examples:
      latita create headless --name myvm
      latita create headless --name webdev --net --capsule code-server
      latita create headless --name bigvm --cpus 4 --memory 8192 --disk 40G
      latita create headless --advanced   # interactive with resource overrides
      latita create headless --full       # full wizard with all options

    See [dim]latita template list[/dim] to see available templates.'''
    # Check for project-level .latita config
    project_cfg = load_project_config()
    clear_project_config()

    if full:
        _interactive_create(level="full")
        return
    if advanced:
        _interactive_create(level="advanced")
        return

    overrides = _build_overrides(
        cpus=cpus,
        memory=memory,
        disk=disk,
        net=net,
        allow_host=allow_host,
        transient=transient,
        destroy_on_stop=ephemeral,
        max_runs=max_runs,
        expires=expires,
        no_guest_agent=no_guest_agent,
        no_selinux=no_selinux,
        restrict_network=restrict_network,
    )

    # Merge project config overrides
    if project_cfg:
        from .operations import _deep_update
        _deep_update(overrides, project_cfg)

    create_instance(template, name=name, capsule_names=capsule or None, overrides=overrides, wait=not no_wait)


@app.command(name="run", rich_help_panel="VM Lifecycle")
def run_cmd(
    template: str = typer.Argument(..., help="Template name (e.g. headless, desktop)"),
    command: list[str] = typer.Argument(None, help="Command to run inside the VM"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="VM name prefix"),
    capsule: list[str] = typer.Option([], "--capsule", "-c", help="Capsule to apply (repeatable)"),
    cpus: Optional[int] = typer.Option(None, "--cpus", help="Override vCPUs"),
    memory: Optional[int] = typer.Option(None, "--memory", "-m", help="Override memory (MiB)"),
    disk: Optional[str] = typer.Option(None, "--disk", "-d", help="Override disk size"),
    net: bool = typer.Option(False, "--net", help="Enable NAT networking"),
    allow_host: list[str] = typer.Option([], "--allow-host", help="Allowed egress host (repeatable)"),
    restrict_network: bool = typer.Option(False, "--restrict-network", help="Restrict outbound network"),
) -> None:
    '''Run a one-shot ephemeral VM (auto-cleaned on shutdown).

    Examples:
      latita run headless -- uname -a
      latita run headless --net -- python3 --version
      latita run headless --name test-001 --cpus 2 --memory 4096 -- echo hello
      latita run headless --capsule tailscale -- curl -s ifconfig.me

    No persistent state — VM is destroyed when the command exits or you Ctrl-C.'''
    overrides = _build_overrides(
        cpus=cpus,
        memory=memory,
        disk=disk,
        net=net,
        allow_host=allow_host,
        restrict_network=restrict_network,
    )
    if name:
        overrides["name"] = name
    run_instance(
        template_name=template,
        command=command or None,
        overrides=overrides,
        capsule_names=capsule or None,
    )


@app.command(name="start", rich_help_panel="VM Lifecycle")
def start_cmd(
    name: str,
    no_wait: bool = typer.Option(False, "--no-wait", help="Return immediately without waiting for initialization"),
) -> None:
    """Start a VM (also: up)."""
    start_instance(name, wait=not no_wait)


@app.command(name="up", hidden=True)
def up_cmd(name: str) -> None:
    """Alias for 'start'."""
    start_instance(name)


@app.command(name="stop", rich_help_panel="VM Lifecycle")
def stop_cmd(name: str) -> None:
    """Stop a VM. Ephemeral VMs are destroyed (also: down)."""
    stop_instance(name)


@app.command(name="down", hidden=True)
def down_cmd(name: str) -> None:
    """Alias for 'stop'."""
    stop_instance(name)


@app.command(name="destroy", rich_help_panel="VM Lifecycle")
def destroy_cmd(
    name: str,
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Destroy a VM and shred its overlay (also: rm, del)."""
    if not force:
        if not typer.confirm(f"Destroy VM '{name}' and shred its disk?", default=False):
            raise typer.Abort()
    destroy_instance(name)


@app.command(name="orphan-clean", rich_help_panel="VM Lifecycle")
def orphan_clean_cmd(
    force: bool = typer.Option(False, "--force", "-f", help="Clean up without asking"),
) -> None:
    """Find and destroy orphaned latita VMs (disk path exists but no metadata)."""
    from .operations import scan_instances
    entries = scan_instances()
    orphans = [e for e in entries if e.get("source") == "orphaned"]
    if not orphans:
        console.print("No orphaned VMs found.", style="green")
        return
    console.print(f"[yellow]Found {len(orphans)} orphaned VM(s):[/yellow]")
    for e in orphans:
        console.print(f"  - {e['name']}  (status: {e.get('status', '?')})")
    if not force:
        if not typer.confirm(f"\nDestroy all {len(orphans)} orphaned VM(s)? This cannot be undone.", default=False):
            raise typer.Abort()
    for e in orphans:
        name = e["name"]
        console.print(f"  Destroying {name}...", style="dim")
        try:
            destroy_instance(name)
        except Exception as ex:
            console.print(f"  [red]Failed to destroy {name}: {ex}[/red]")
    console.print(f"[green]Cleaned up {len(orphans)} orphaned VM(s).[/green]")


@app.command(name="rm", hidden=True)
def rm_cmd(
    name: str,
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Alias for 'destroy'."""
    if not force:
        if not typer.confirm(f"Destroy VM '{name}' and shred its disk?", default=False):
            raise typer.Abort()
    destroy_instance(name)


@app.command(name="del", hidden=True)
def del_cmd(
    name: str,
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
) -> None:
    """Alias for 'destroy'."""
    if not force:
        if not typer.confirm(f"Destroy VM '{name}' and shred its disk?", default=False):
            raise typer.Abort()
    destroy_instance(name)


@app.command(name="revive", rich_help_panel="VM Lifecycle")
def revive_cmd(name: str) -> None:
    """Revive a stored VM (re-create libvirt domain from saved metadata)."""
    revive_instance(name)


@app.command(name="ssh", rich_help_panel="Access")
def ssh_cmd(
    name: str,
    command: Optional[str] = typer.Argument(None, help="Optional command to run"),
) -> None:
    """SSH into a VM."""
    ssh_instance(name, command=command)


@app.command(name="connect", rich_help_panel="Access")
def connect_cmd(name: str) -> None:
    """Connect to a VM (SPICE for desktop, SSH for headless)."""
    connect_instance(name)


@app.command(name="status", rich_help_panel="VMs")
def status_cmd(name: str) -> None:
    """Show VM state and initialization progress."""
    from .libvirt import get_vm_state
    state = get_vm_state(name)
    init = get_vm_init_state(name)
    console.print(f"[bold]{name}[/bold]")
    console.print(f"  State      : {state or 'unknown'}")
    console.print(f"  Cloud-init : {init['cloud_init']}")
    if init['desktop'] != 'n/a':
        console.print(f"  Desktop    : {init['desktop']}")
    overall_color = "green" if init['overall'] == "ready" else "red" if init['overall'] == "failed" else "yellow"
    console.print(f"  Overall    : [{overall_color}]{init['overall']}[/{overall_color}]")


@app.command(name="wait", rich_help_panel="VMs")
def wait_cmd(name: str) -> None:
    """Block until a VM finishes initialization."""
    ok = wait_for_vm_ready(name)
    if not ok:
        raise typer.Exit(1)


@app.command(name="list", rich_help_panel="VMs")
def list_cmd() -> None:
    """List all VMs (also: ls)."""
    list_instances()


@app.command(name="ls", hidden=True)
def ls_cmd() -> None:
    """Alias for 'list'."""
    list_instances()


@app.command(name="ps", rich_help_panel="VMs")
def ps_cmd() -> None:
    """List running VMs (compact format)."""
    from rich.table import Table
    entries = [e for e in scan_instances() if e.get("status") == "running"]
    if not entries:
        console.print("No running VMs", style="yellow")
        return
    table = Table(title="Running VMs")
    table.add_column("NAME")
    table.add_column("TEMPLATE")
    table.add_column("IP")
    table.add_column("CPUS")
    table.add_column("MEM")
    for e in entries:
        table.add_row(
            e["name"],
            e["template"] or e["profile"],
            e["ip"] or e["mgmt_ip"] or "-",
            str(e.get("cpus", "-")),
            str(e.get("memory", "-")),
        )
    console.print(table)


@app.command(name="bootstrap", rich_help_panel="Host")
def bootstrap_cmd() -> None:
    """Bootstrap the host (networks, keys, base image)."""
    bootstrap_host()


@app.command(name="init-base", rich_help_panel="Host")
def init_base_cmd(
    name: Optional[str] = typer.Option(None, "--name", "-n"),
    url: Optional[str] = typer.Option(None, "--url", "-u"),
) -> None:
    """Download a base cloud image."""
    init_base(name, url)


@app.command(name="doctor", rich_help_panel="Host")
def doctor_cmd(install: bool = False) -> None:
    """Check host dependencies. Use --install to attempt automatic fixes."""
    from latita.operations import doctor_install
    if install:
        doctor_install()
    else:
        doctor()


# ---------------------------------------------------------------------------
# Capsule commands
# ---------------------------------------------------------------------------

@capsule_app.command(name="list")
def capsule_list_cmd() -> None:
    """List available capsules (also: ls)."""
    all_caps = list_capsules()
    if not all_caps:
        console.print("No capsules found", style="yellow")
        return
    caps_mod.format_capsule_table(all_caps)


@capsule_app.command(name="ls", hidden=True)
def capsule_ls_cmd() -> None:
    """Alias for 'capsule list'."""
    capsule_list_cmd()


@capsule_app.command(name="apply")
def capsule_apply_cmd(
    vm: str = typer.Argument(..., help="VM name"),
    capsule: str = typer.Argument(..., help="Capsule name"),
) -> None:
    """Apply a capsule to a running VM via SSH."""
    apply_capsule_live(vm, capsule)


# ---------------------------------------------------------------------------
# Template commands
# ---------------------------------------------------------------------------

@template_app.command(name="list")
def template_list_cmd() -> None:
    """List available .latita templates (also: ls)."""
    templates = list_latita_templates()
    if not templates:
        console.print("No templates found", style="yellow")
        return
    from rich.table import Table
    table = Table(title="Templates")
    table.add_column("Name")
    table.add_column("Profile")
    table.add_column("OS")
    table.add_column("CPUs")
    table.add_column("Memory")
    table.add_column("Disk")
    table.add_column("Description")
    for name, data in templates.items():
        cpus = data.get("cpus", "-")
        memory = data.get("memory", "-")
        disk = data.get("disk_size", "-")
        os_family = data.get("os_family", "-")
        desc = str(data.get("description", "")).strip() or "-"
        table.add_row(
            name,
            str(data.get("profile", "-")),
            os_family,
            str(cpus),
            str(memory) if memory != "-" else "-",
            str(disk) if disk != "-" else "-",
            desc,
        )
    console.print(table)


@template_app.command(name="ls", hidden=True)
def template_ls_cmd() -> None:
    """Alias for 'template list'."""
    template_list_cmd()


@template_app.command(name='show')
def template_show_cmd(name: str) -> None:
    '''Show a template's contents (full YAML).'''
    data = load_latita_template(name)
    console.print(f'[bold]{name}.latita[/bold]')
    console.print(data)


@template_app.command(name='generate')
def template_generate_cmd(
    output: Path = typer.Option(..., '--output', '-o', help='Output file path (e.g. mytemplate.latita)'),
) -> None:
    '''Interactively generate a new .latita template file.'''
    try:
        recipe = interactive_generate_template()
    except typer.Abort:
        console.print('Aborted.', style='yellow')
        raise typer.Exit()
    write_yaml(output, recipe)
    console.print(f'[green]Template written to {output}[/green]')


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------

@config_app.command(name='show')
def config_show_cmd() -> None:
    """Show current latita configuration."""
    cfg = get_config()
    console.print('[bold]Latita configuration[/bold]\n')
    console.print(f'  Root directory: {cfg.root_dir}')
    console.print(f'  Libvirt URI:  {cfg.libvirt_uri}')
    if cfg.is_session:
        console.print('  Mode:         [green]Session[/green] (unprivileged, isolated per VM)')
    else:
        console.print('  Mode:         [yellow]System[/yellow] (privileged, shared NAT bridge)')
    console.print(f'  Default base: {cfg.default_base_name}')
    console.print(f'  Base images:  {cfg.base_dir}')
    root_cfg = load_root_config(cfg.root_dir)
    if root_cfg.get("libvirt_uri"):
        console.print(f'\n  [dim]Mode override set in {cfg.root_dir}/.latita[/dim]')
    console.print('\n[dim]Use "latita config set-mode session|system" to switch.[/dim]')
    console.print('[dim]Or set LIBVIRT_DEFAULT_URI environment variable.[/dim]')


@config_app.command(name='set-mode')
def config_set_mode_cmd(
    mode: str = typer.Argument(..., help="session or system"),
) -> None:
    """Switch between session and system libvirt mode."""
    cfg = get_config()
    mode = mode.lower().strip()
    if mode not in ("session", "system"):
        console.print(f"[red]Invalid mode '{mode}'. Use 'session' or 'system'.[/red]")
        raise typer.Exit(1)

    uri = f"qemu:///{mode}"
    root_cfg = load_root_config(cfg.root_dir)
    root_cfg["libvirt_uri"] = uri
    config_path = cfg.root_dir / ".latita"
    write_yaml(config_path, root_cfg)
    clear_root_config()

    console.print(f"[green]Mode set to {mode} ({uri})[/green]")
    console.print(f"Config saved to {config_path}")
    console.print("[yellow]Restart latita for the change to take effect.[/yellow]")


@config_app.command(name='reset-mode')
def config_reset_mode_cmd() -> None:
    """Remove the persisted mode override and go back to auto-detection."""
    cfg = get_config()
    config_path = cfg.root_dir / ".latita"
    if config_path.exists():
        root_cfg = load_root_config(cfg.root_dir)
        root_cfg.pop("libvirt_uri", None)
        if root_cfg:
            write_yaml(config_path, root_cfg)
        else:
            config_path.unlink()
        clear_root_config()
        console.print("[green]Mode override removed. Auto-detection restored.[/green]")
    else:
        console.print("[yellow]No mode override found.[/yellow]")
    console.print("[yellow]Restart latita for the change to take effect.[/yellow]")


# ---------------------------------------------------------------------------
# Dashboard command
# ---------------------------------------------------------------------------

@dashboard_app.command(name='run')
def dashboard_run_cmd() -> None:
    '''Launch the TUI dashboard (htop-like, live VM monitoring).'''
    from .tui import Dashboard
    app_instance = Dashboard()
    app_instance.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()
