from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import warnings
from pathlib import Path
from typing import Any, Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
)
from rich.console import Console
from rich.pretty import Pretty

from .config import (
    BASE_IMAGES,
    get_capsule_path,
    get_config,
    get_template_path,
    is_builtin_capsule,
    is_builtin_template,
    list_capsules,
    list_latita_templates,
    write_yaml,
)
from .operations import (
    _detect_video_models,
    _maybe_download_base,
    apply_capsule_live,
    bootstrap_host,
    connect_instance,
    create_instance,
    destroy_instance,
    doctor,
    fetch_vm_error_log,
    get_vm_init_state,
    pause_instance,
    resume_instance,
    run_instance,
    scan_instances,
    ssh_instance,
    start_instance,
    stop_instance,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_editor(path: Any) -> None:
    """Open a file in $EDITOR."""
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(path)])


def _ensure_running(name: str) -> None:
    """Start a VM if it is not already running."""
    entries = {e["name"]: e for e in scan_instances()}
    if entries.get(name, {}).get("status") != "running":
        start_instance(name)


# ---------------------------------------------------------------------------
# Action list items
# ---------------------------------------------------------------------------

class ActionItem(ListItem):
    def __init__(self, action_id: str, label: str, **kwargs: Any) -> None:
        super().__init__(Label(label), **kwargs)
        self.action_id = action_id


class GroupHeader(ListItem):
    def __init__(self, label: str, **kwargs: Any) -> None:
        super().__init__(Label(label), **kwargs)
        self.action_id = None


# ---------------------------------------------------------------------------
# Confirm modal
# ---------------------------------------------------------------------------

class ConfirmScreen(Screen):
    """Simple Yes/No modal. 'No' is the default focus to prevent accidental confirms."""

    BINDINGS = [
        Binding("y", "yes", "Yes", show=False),
        Binding("n", "no", "No", show=False),
        Binding("escape", "no", "No", show=False),
        Binding("left", "focus_prev_button", "Prev", show=False),
        Binding("right", "focus_next_button", "Next", show=False),
    ]

    def __init__(self, message: str, on_result: Callable[[bool], None]) -> None:
        super().__init__()
        self.message = message
        self.on_result = on_result

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.message, id="confirm-msg")
            with Horizontal(id="confirm-buttons", classes="form-buttons"):
                yield Button("No", id="btn-no", variant="primary")
                yield Button("Yes", id="btn-yes", variant="error")

    def on_mount(self) -> None:
        self.query_one("#btn-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-yes":
            self.action_yes()
        elif event.button.id == "btn-no":
            self.action_no()

    def action_focus_next_button(self) -> None:
        no_btn = self.query_one("#btn-no", Button)
        yes_btn = self.query_one("#btn-yes", Button)
        if self.focused is no_btn:
            yes_btn.focus()
        elif self.focused is yes_btn:
            no_btn.focus()

    def action_focus_prev_button(self) -> None:
        self.action_focus_next_button()

    def action_yes(self) -> None:
        self.app.pop_screen()
        self.on_result(True)

    def action_no(self) -> None:
        self.app.pop_screen()
        self.on_result(False)


class PromptScreen(Screen):
    """Native TUI prompt for a single-line text value. Stays inside the TUI."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
    ]

    def __init__(self, label: str, placeholder: str, on_result: Callable[[str | None], None]) -> None:
        super().__init__()
        self.label = label
        self.placeholder = placeholder
        self.on_result = on_result

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box", classes="form-box"):
            yield Static(self.label, id="prompt-label", classes="form-title")
            yield Input(placeholder=self.placeholder, id="prompt-input")
            yield Static("", id="prompt-error", classes="form-error")
            with Horizontal(id="prompt-buttons", classes="form-buttons"):
                yield Button("OK", id="btn-ok", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-ok":
            self._submit()
        elif event.button.id == "btn-cancel":
            self.action_dismiss()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "prompt-input":
            self._submit()

    def _submit(self) -> None:
        value = self.query_one("#prompt-input", Input).value.strip()
        if not value:
            self.query_one("#prompt-error", Static).update("Value is required")
            self.query_one("#prompt-input", Input).focus()
            return
        self.app.pop_screen()
        self.on_result(value)

    def action_dismiss(self) -> None:
        self.app.pop_screen()
        self.on_result(None)


class TypeToConfirmScreen(Screen):
    """Type a confirmation word + Enter to proceed. Prevents accidental Enter spam."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, message: str, confirm_word: str, on_result: Callable[[bool], None]) -> None:
        super().__init__()
        self.message = message
        self.confirm_word = confirm_word
        self.on_result = on_result

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.message, id="confirm-msg")
            yield Static(f"Type '{self.confirm_word}' and press Enter to confirm. Esc to cancel.", id="confirm-hint")
            yield Input(placeholder=f"type: {self.confirm_word}", id="confirm-input")
            yield Static("", id="confirm-error")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "confirm-input":
            self._check()

    def _check(self) -> None:
        inp = self.query_one("#confirm-input", Input)
        err = self.query_one("#confirm-error", Static)
        if inp.value.strip().lower() == self.confirm_word.lower():
            self.app.pop_screen()
            self.on_result(True)
        else:
            err.update(f"Wrong. Expected '{self.confirm_word}'.")
            inp.value = ""
            inp.focus()

    def action_cancel(self) -> None:
        self.app.pop_screen()
        self.on_result(False)


# ---------------------------------------------------------------------------
# Form screen base (Create VM / Run VM)
# ---------------------------------------------------------------------------

class FormScreen(Screen[dict[str, Any] | None]):
    """Base modal form with profile, name, network, video model, error, buttons."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
        Binding("left", "focus_prev_button", "Prev", show=False),
        Binding("right", "focus_next_button", "Next", show=False),
    ]

    _name_counters: dict[str, int] = {}

    def __init__(self, title: str, box_id: str) -> None:
        super().__init__()
        self._title = title
        self._box_id = box_id
        self._video_options = self._load_video_options()
        self._templates = list_latita_templates()
        self._last_template = "headless"

    @staticmethod
    def _load_video_options() -> tuple[list[tuple[str, str]], str]:
        """Probe QEMU and return (Select options, default value)."""
        try:
            models = _detect_video_models()
        except Exception:
            return ([("Auto-detect", "")], "")
        available = models["available"]
        best = models["best"]
        labels = {
            "qxl": "qxl   (best SPICE)",
            "virtio": "virtio (good perf)",
            "vga": "vga    (universal)",
        }
        opts = []
        default = ""
        for model in ("qxl", "virtio", "vga"):
            if available.get(model):
                opts.append((labels[model], model))
                if model == best:
                    default = model
        if not opts:
            opts = [("Auto-detect", "")]
        return (opts, default)

    def _suggest_name(self, template: str) -> str:
        """Return the next sequential name for a template."""
        self._name_counters[template] = self._name_counters.get(template, 0) + 1
        return f"{template}-{self._name_counters[template]}"

    def _update_name_on_template_change(self, new_template: str) -> None:
        """Smart name update: keep custom names, replace auto-generated ones."""
        name_widget = self.query_one("#name", Input)
        current = name_widget.value.strip()
        if not current:
            name_widget.value = self._suggest_name(new_template)
        elif self._is_auto_generated_name(current, self._last_template):
            name_widget.value = self._suggest_name(new_template)
        # else: custom name — leave it alone

    @staticmethod
    def _is_auto_generated_name(name: str, template: str) -> bool:
        import re
        return bool(re.fullmatch(rf"{re.escape(template)}-\d+", name))

    def _template_profile(self, template_name: str) -> str:
        """Return the profile (headless/desktop) for a template name."""
        data = self._templates.get(template_name, {})
        return str(data.get("profile", "headless")).lower()

    def compose(self) -> ComposeResult:
        with Vertical(id=self._box_id, classes="form-box"):
            yield Static(self._title, id="form-title", classes="form-title")
            with Horizontal(id="form-body"):
                with Vertical(id="form-left"):
                    yield from self._compose_fields()
                    yield Static("", id="form-error", classes="form-error")
                    with Horizontal(id="form-buttons", classes="form-buttons"):
                        yield Button("Create", id="btn-create", variant="primary")
                        yield Button("Cancel", id="btn-cancel")
                        yield Button("Advanced ▶", id="btn-advanced")
                with Vertical(id="form-right"):
                    yield Input(value="dev", id="guest_user")
                    yield Input(value="latita", password=True, id="password")
                    yield Checkbox("Passwordless sudo", value=True, id="passwordless_sudo")

    def _compose_fields(self) -> ComposeResult:
        """Child classes override to yield extra widgets."""
        template_names = sorted(self._templates.keys())
        if not template_names:
            template_names = ["headless"]
        template_options = [(n, n) for n in template_names]
        default_template = template_options[0][1] if template_options else "headless"

        yield Input(placeholder="VM name", id="name")
        yield Select(template_options, value=default_template, id="profile")
        yield Select(
            [("NAT (shared with host)", "nat"), ("Isolated (no internet)", "isolated"), ("None (no network device)", "none")],
            value="nat",
            id="network_mode",
        )
        video_opts, video_default = self._video_options
        yield Select(video_opts, value=video_default or None, id="video_model")
        yield Static("Video model (desktop VMs only)", id="video-hint", classes="form-hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-advanced":
            self._toggle_advanced()
        elif event.button.id == "btn-create":
            self.action_submit()
        elif event.button.id == "btn-cancel":
            self.action_dismiss()

    def _toggle_advanced(self) -> None:
        right = self.query_one("#form-right", Vertical)
        btn = self.query_one("#btn-advanced", Button)
        if right.styles.display == "none":
            right.styles.width = "auto"
            right.styles.display = "block"
            btn.label = "Advanced ◀"
        else:
            right.styles.display = "none"
            btn.label = "Advanced ▶"

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "profile":
            new_template = str(event.value) if event.value else "headless"
            self._toggle_video_visibility(new_template)
            self._update_name_on_template_change(new_template)
            self._last_template = new_template

    def action_focus_next_button(self) -> None:
        """Toggle focus between Create and Cancel buttons."""
        create_btn = self.query_one("#btn-create", Button)
        cancel_btn = self.query_one("#btn-cancel", Button)
        if self.focused is create_btn:
            cancel_btn.focus()
        elif self.focused is cancel_btn:
            create_btn.focus()

    def action_focus_prev_button(self) -> None:
        """Toggle focus between Create and Cancel buttons."""
        self.action_focus_next_button()

    def _toggle_video_visibility(self, template_name: str) -> None:
        video = self.query_one("#video_model", Select)
        hint = self.query_one("#video-hint", Static)
        profile = self._template_profile(template_name)
        if profile == "desktop":
            video.styles.display = "block"
            hint.styles.display = "block"
        else:
            video.styles.display = "none"
            hint.styles.display = "none"

    def on_mount(self) -> None:
        name_widget = self.query_one("#name", Input)
        name_widget.focus()
        profile_widget = self.query_one("#profile", Select)
        current_template = str(profile_widget.value) if profile_widget.value else "headless"
        self._toggle_video_visibility(current_template)
        # Hide advanced panel by default
        right = self.query_one("#form-right", Vertical)
        right.styles.width = "0"
        right.styles.display = "none"

    def action_submit(self) -> None:
        name_widget = self.query_one("#name", Input)
        profile_widget = self.query_one("#profile", Select)
        net_widget = self.query_one("#network_mode", Select)
        error_widget = self.query_one("#form-error", Static)

        name = name_widget.value.strip()
        template_name = str(profile_widget.value) if profile_widget.value else "headless"
        net_mode = str(net_widget.value) if net_widget.value else "nat"

        if not name:
            error_widget.update("Name is required")
            name_widget.focus()
            return
        result = self._build_result(name, template_name, net_mode)
        self.dismiss(result)

    def _build_result(self, name: str, template_name: str, net_mode: str) -> dict[str, Any]:
        """Child classes override to add extra fields."""
        profile = self._template_profile(template_name)
        template_data = self._templates.get(template_name, {})
        recipe: dict[str, Any] = {
            "profile": profile,
            "template_name": template_name,
            "name": name,
            "network": {
                "mode": net_mode,
                "nat_network": "default" if net_mode == "nat" else "",
            },
        }
        if "base_image" in template_data:
            recipe["base_image"] = template_data["base_image"]
        if profile == "desktop":
            video_widget = self.query_one("#video_model", Select)
            video = str(video_widget.value) if video_widget.value else ""
            if video:
                recipe["video_model"] = video

        # Advanced fields (always include so the CLI can use them)
        guest_user_widget = self.query_one("#guest_user", Input)
        password_widget = self.query_one("#password", Input)
        sudo_widget = self.query_one("#passwordless_sudo", Checkbox)

        guest_user = guest_user_widget.value.strip() or "dev"
        recipe["guest_user"] = guest_user
        recipe["passwordless_sudo"] = sudo_widget.value

        # Hash password for desktop profiles that need it
        if profile == "desktop" and template_name != "desktop-minimal":
            password = password_widget.value
            if password:
                from .utils import hash_password
                recipe["login_hash"] = hash_password(password)

        return recipe

    def action_dismiss(self) -> None:
        self.dismiss(None)


class CreateVMScreen(FormScreen):
    """Native TUI form for creating a persistent VM."""

    def __init__(self) -> None:
        super().__init__("Create VM", "create-box")

    def _compose_fields(self) -> ComposeResult:
        yield from super()._compose_fields()
        yield Checkbox("Transient (auto-remove on shutdown)", value=False, id="transient")
        yield Checkbox("Destroy on stop", value=False, id="destroy_on_stop")

    def _build_result(self, name: str, profile: str, net_mode: str) -> dict[str, Any]:
        recipe = super()._build_result(name, profile, net_mode)
        transient = self.query_one("#transient", Checkbox).value
        destroy = self.query_one("#destroy_on_stop", Checkbox).value
        if transient:
            recipe.setdefault("ephemeral", {})["transient"] = True
        if destroy:
            recipe.setdefault("ephemeral", {})["destroy_on_stop"] = True
        return {"mode": "create", "recipe": recipe}


class RunVMScreen(Screen[dict[str, Any] | None]):
    """Minimal one-shot VM — pick a template, hit Run."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
        Binding("e", "edit_template", "Edit", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._templates = list_latita_templates()

    def compose(self) -> ComposeResult:
        template_names = sorted(self._templates.keys())
        if not template_names:
            template_names = ["headless"]
        template_options = [(n, n) for n in template_names]
        default_template = template_options[0][1] if template_options else "headless"

        with Vertical(id="run-box", classes="form-box"):
            yield Static("Run one-shot VM", id="form-title", classes="form-title")
            yield Select(template_options, value=default_template, id="run-profile")
            yield Static("", id="run-defaults", classes="form-hint")
            yield Static("Transient, destroyed on shutdown", id="run-warn", classes="form-warn")
            with Horizontal(id="form-buttons", classes="form-buttons"):
                yield Button("Run", id="btn-run", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#run-profile", Select).focus()
        self._update_defaults()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "run-profile":
            self._update_defaults()

    def _update_defaults(self) -> None:
        sel = self.query_one("#run-profile", Select)
        template_name = str(sel.value) if sel.value else "headless"
        data = self._templates.get(template_name, {})
        defaults_widget = self.query_one("#run-defaults", Static)
        profile = data.get("profile", "headless")
        cpus = data.get("cpus", "?")
        mem = data.get("memory", "?")
        net = data.get("network", {}).get("mode", "user" if get_config().is_session else "nat")
        base = data.get("base_image", "?")
        defaults_widget.update(
            f"profile={profile}  cpus={cpus}  mem={mem}  net={net}  base={base}  [E] Edit"
        )

    def _template_profile(self, template_name: str) -> str:
        data = self._templates.get(template_name, {})
        return str(data.get("profile", "headless")).lower()

    def action_edit_template(self) -> None:
        sel = self.query_one("#run-profile", Select)
        template_name = str(sel.value) if sel.value else "headless"
        path = get_template_path(template_name)
        if path and path.exists():
            _open_editor(path)
        else:
            self.notify(f"Template file not found for '{template_name}'", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-run":
            self.action_submit()
        elif event.button.id == "btn-cancel":
            self.action_dismiss()

    def action_submit(self) -> None:
        profile_widget = self.query_one("#run-profile", Select)
        template_name = str(profile_widget.value) if profile_widget.value else "headless"
        profile = self._template_profile(template_name)
        template_data = self._templates.get(template_name, {})
        name = f"{template_name}-run"
        net_mode = "user" if get_config().is_session else "nat"
        recipe: dict[str, Any] = {
            "profile": profile,
            "template_name": template_name,
            "name": name,
            "network": {"mode": net_mode, "nat_network": "default" if net_mode == "nat" else ""},
            "ephemeral": {"transient": True, "destroy_on_stop": True},
        }
        if "base_image" in template_data:
            recipe["base_image"] = template_data["base_image"]
        self.dismiss({"mode": "run", "recipe": recipe})

    def action_dismiss(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Apply capsule screen
# ---------------------------------------------------------------------------

class ApplyCapsuleScreen(Screen[str | None]):
    """Native TUI picker for applying a capsule."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
    ]

    def __init__(self, vm_name: str) -> None:
        super().__init__()
        self.vm_name = vm_name
        self._net_warn = self._check_network(vm_name)

    def _check_network(self, name: str) -> str | None:
        from .metadata import read_instance_recipe, read_instance_spec
        recipe = read_instance_recipe(name)
        spec = read_instance_spec(name)
        net_mode = ""
        if recipe:
            net_mode = recipe.get("network", {}).get("mode", "")
        elif spec:
            net_mode = spec.get("net_mode", "")
        if net_mode in ("isolated", "none", ""):
            return f"Warning: VM has no internet ({net_mode or 'unknown'}). Capsules that download will fail."
        return None

    def compose(self) -> ComposeResult:
        with Vertical(id="cap-box", classes="form-box"):
            yield Static(f"Apply capsule to {self.vm_name}", id="cap-title", classes="form-title")
            if self._net_warn:
                yield Static(
                    "\n".join([
                        "NETWORK WARNING",
                        self._net_warn,
                        "Capsules that download will fail!",
                    ]),
                    id="cap-net-warn",
                )
            caps = list(list_capsules().keys())
            if caps:
                yield Select([(c, c) for c in caps], value=caps[0], id="capsule")
            else:
                yield Static("No capsules available", id="cap-none")
            yield Static("Tab to navigate, Space/Enter to activate", id="cap-hint")
            with Horizontal(id="cap-buttons", classes="form-buttons"):
                yield Button("Apply", id="btn-apply", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-apply":
            self.action_submit()
        elif event.button.id == "btn-cancel":
            self.action_dismiss()

    def action_submit(self) -> None:
        caps = list(self.query("#capsule"))
        if not caps:
            self.dismiss(None)
            return
        cap_widget = caps[0]
        assert isinstance(cap_widget, Select)
        value = cap_widget.value
        if value is None:
            self.notify("Select a capsule first", severity="warning")
            return
        self.dismiss(str(value))

    def action_dismiss(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Info screen
# ---------------------------------------------------------------------------

class InfoScreen(Screen):
    """Show detailed metadata for a VM."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=False),
        Binding("q", "app.pop_screen", "Back", show=False),
    ]

    def __init__(self, vm_entry: dict[str, Any]) -> None:
        super().__init__()
        self.vm_entry = vm_entry

    def compose(self) -> ComposeResult:
        name = self.vm_entry.get("name", "unknown")
        with Vertical(id="info-box"):
            yield Static(f"VM Info: {name}", id="info-title")
            with ScrollableContainer(id="info-scroll"):
                yield Static("", id="info-detail")
            yield Static("[Esc/q] Back", id="info-hint")

    def on_mount(self) -> None:
        detail = self.query_one("#info-detail", Static)
        detail.update(Pretty(self._build_detail()))

    def _build_detail(self) -> dict[str, Any]:
        from .metadata import read_instance_spec, read_instance_recipe
        e = self.vm_entry
        name = e.get("name", "unknown")
        spec = read_instance_spec(name)
        recipe = read_instance_recipe(name)
        detail: dict[str, Any] = {
            "name": name,
            "status": e.get("status", "?"),
            "ip": e.get("ip") or e.get("mgmt_ip") or "—",
            "profile": e.get("profile", "?"),
            "template": e.get("template", "?"),
            "cpus": e.get("cpus", "?"),
            "memory": e.get("memory", "?"),
            "applied_capsules": e.get("applied_capsules", []),
        }
        if spec:
            detail.update({
                "transient": spec.get("transient", False),
                "destroy_on_stop": spec.get("destroy_on_stop", False),
                "max_runs": spec.get("max_runs"),
                "run_count": spec.get("run_count", 0),
                "expire_at": spec.get("expire_at"),
                "created_at": spec.get("created_at"),
                "base_image": spec.get("base_image", "?"),
                "net_mode": spec.get("net_mode", "?"),
                "graphics": spec.get("graphics", "none"),
            })
        if recipe:
            detail["os_family"] = recipe.get("os_family", "?")
            detail["guest_user"] = recipe.get("guest_user", "?")
        return detail


class LogsScreen(Screen):
    """Show diagnostic logs for a VM."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=False),
        Binding("q", "app.pop_screen", "Back", show=False),
    ]

    def __init__(self, name: str, log_text: str) -> None:
        super().__init__()
        self._name = name
        self._log_text = log_text

    def compose(self) -> ComposeResult:
        with Vertical(id="logs-box"):
            yield Static(f"Logs: {self._name}", id="logs-title")
            with ScrollableContainer(id="logs-scroll"):
                yield Static(self._log_text, id="logs-content", markup=False)
            yield Static("[Esc/q] Back", id="logs-hint")


class BaseImagePickerScreen(Screen[str | None]):
    """Pick a base image to download from the catalog."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel", show=False),
    ]

    def __init__(self, missing_image: str, on_result: Callable[[str | None], None]) -> None:
        super().__init__()
        self._missing = missing_image
        self._on_result = on_result

    def compose(self) -> ComposeResult:
        with Vertical(id="image-box"):
            yield Static(f"Base image '{self._missing}' not found", id="image-title")
            yield Static("Select an image to download:", id="image-hint2", classes="form-hint")
            options = []
            for label, data in BASE_IMAGES.items():
                cached = (get_config().base_dir / data["filename"]).exists()
                tag = " (cached)" if cached else ""
                options.append((f"{label}{tag}", data["filename"]))
            if options:
                yield Select(options, value=options[0][1], id="image-select")
            else:
                yield Static("No images in catalog", classes="form-error")
            with Horizontal(classes="form-buttons"):
                yield Button("Download", id="btn-download", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-download":
            sel = self.query_one("#image-select", Select)
            filename = str(sel.value) if sel.value else None
            self._on_result(filename)
            self.dismiss(filename)
        elif event.button.id == "btn-cancel":
            self._on_result(None)
            self.dismiss(None)


# ---------------------------------------------------------------------------
# Browser screen base (Templates / Capsules)
# ---------------------------------------------------------------------------

class BrowserScreen(Screen):
    """Base two-pane browser with list, detail, and CRUD actions."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("q", "app.pop_screen", "Back", show=False),
        Binding("tab", "toggle_pane", "Toggle pane", show=True),
        Binding("e", "edit", "Edit", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("r", "rename", "Rename", show=True),
        Binding("y", "duplicate", "Duplicate", show=True),
        Binding("n", "new", "New", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._items: dict[str, Any] = {}

    # --- Abstract hooks ---

    def _browser_title(self) -> str:
        raise NotImplementedError

    def _table_columns(self) -> list[str]:
        raise NotImplementedError

    def _load_items(self) -> dict[str, Any]:
        raise NotImplementedError

    def _detail_for(self, name: str) -> Any:
        raise NotImplementedError

    def _is_builtin(self, name: str) -> bool:
        raise NotImplementedError

    def _get_path(self, name: str) -> Path:
        raise NotImplementedError

    def _file_ext(self) -> str:
        raise NotImplementedError

    def _new_schema(self) -> dict[str, Any]:
        raise NotImplementedError

    def _copy_builtin(self, name: str, dst: Path) -> None:
        raise NotImplementedError

    def _user_dir(self) -> Path:
        raise NotImplementedError

    # --- Compose & lifecycle ---

    def compose(self) -> ComposeResult:
        yield Static(self._browser_title(), id="browser-title")
        with Horizontal(id="browser-body"):
            yield DataTable(id="browser-left", cursor_type="row")
            with Vertical(id="browser-right"):
                with ScrollableContainer(id="browser-detail-scroll"):
                    yield Static("Select an item.\n", id="browser-detail")
                yield Static(
                    "Shortcuts\n"
                    "  e      Edit in $EDITOR\n"
                    "  d      Delete\n"
                    "  r      Rename\n"
                    "  y      Duplicate\n"
                    "  n      New\n"
                    "  Tab    Toggle list / detail\n"
                    "  Esc    Back",
                    id="browser-actions",
                )

    def on_mount(self) -> None:
        self._refresh_items()
        self.query_one("#browser-left", DataTable).focus()

    # --- Refresh & selection ---

    def _refresh_items(self) -> None:
        table = self.query_one("#browser-left", DataTable)
        table.clear()
        for col in self._table_columns():
            table.add_column(col)
        self._items = self._load_items()
        for name, data in self._items.items():
            table.add_row(*self._row_cells(name, data))
        if table.row_count:
            table.move_cursor(row=0)
            self._show_detail(0)

    def _row_cells(self, name: str, data: dict[str, Any]) -> list[str]:
        return [name]

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = self.query_one("#browser-left", DataTable)
        cursor = table.cursor_row
        if isinstance(cursor, int):
            self._show_detail(cursor)

    def _selected_name(self) -> str | None:
        table = self.query_one("#browser-left", DataTable)
        cursor = table.cursor_row
        if not isinstance(cursor, int):
            return None
        names = list(self._items.keys())
        if 0 <= cursor < len(names):
            return names[cursor]
        return None

    def _show_detail(self, cursor: int) -> None:
        names = list(self._items.keys())
        if 0 <= cursor < len(names):
            name = names[cursor]
            detail = self.query_one("#browser-detail", Static)
            detail.update(Pretty(self._detail_for(name)))

    def _app(self) -> Dashboard | None:
        app = self.app
        return app if isinstance(app, Dashboard) else None

    # --- Actions ---

    def action_toggle_pane(self) -> None:
        table = self.query_one("#browser-left", DataTable)
        scroll = self.query_one("#browser-detail-scroll", ScrollableContainer)
        if self.focused is table:
            scroll.focus()
        else:
            table.focus()

    def action_edit(self) -> None:
        name = self._selected_name()
        if not name:
            return
        app = self._app()
        if not app:
            return
        path = self._get_path(name)
        if self._is_builtin(name):
            cfg = get_config()
            dst = cfg.templates_dir / f"{name}{self._file_ext()}"
            cfg.templates_dir.mkdir(parents=True, exist_ok=True)
            self._copy_builtin(name, dst)
            path = dst
            app.notify(f"Copied built-in to user directory")
        app._run_command(lambda: _open_editor(path), f"Edit {name}")
        self._refresh_items()

    def action_delete(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if self._is_builtin(name):
            self.notify("Cannot delete built-in items", severity="warning")
            return
        app = self._app()
        if not app:
            return

        def _on_result(confirmed: bool) -> None:
            if confirmed:
                self._get_path(name).unlink()
                self._refresh_items()
                app.notify(f"'{name}' deleted")

        self.app.push_screen(ConfirmScreen(f"Delete '{name}'?", _on_result))

    def action_rename(self) -> None:
        name = self._selected_name()
        if not name:
            return
        app = self._app()
        if not app:
            return
        ext = self._file_ext()

        # If built-in, copy to user directory first (same pattern as edit)
        if self._is_builtin(name):
            cfg = get_config()
            dst = cfg.templates_dir / f"{name}{ext}"
            cfg.templates_dir.mkdir(parents=True, exist_ok=True)
            self._copy_builtin(name, dst)
            app.notify(f"Copied built-in to user directory")
            old_path = dst
        else:
            old_path = self._get_path(name)

        def _on_rename(new_name: str | None) -> None:
            if not new_name or new_name == name:
                return
            new_path = old_path.parent / f"{new_name}{ext}"
            if new_path.exists():
                app.notify(f"'{new_name}' already exists", severity="warning")
                return
            old_path.rename(new_path)
            self._refresh_items()
            app.notify(f"Renamed to '{new_name}'")

        self.app.push_screen(
            PromptScreen(f"Rename '{name}'", "new name", _on_rename)
        )

    def action_duplicate(self) -> None:
        name = self._selected_name()
        if not name:
            return
        app = self._app()
        if not app:
            return
        ext = self._file_ext()
        path = self._get_path(name)
        dst = path.parent / f"{name}-copy{ext}"
        if dst.exists():
            app.notify("A copy already exists", severity="warning")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        self._refresh_items()
        app.notify(f"Duplicated to '{dst.stem}'")

    def action_new(self) -> None:
        app = self._app()
        if not app:
            return
        ext = self._file_ext()
        schema = self._new_schema()
        parent = self._user_dir()
        parent.mkdir(parents=True, exist_ok=True)

        # Generate a unique default name (e.g. untitled-1, untitled-2)
        def _unique_name() -> str:
            for i in range(1, 10000):
                candidate = f"untitled-{i}"
                if not (parent / f"{candidate}{ext}").exists():
                    return candidate
            import uuid
            return f"untitled-{uuid.uuid4().hex[:8]}"

        name = _unique_name()
        path = parent / f"{name}{ext}"
        schema.setdefault("description", "My custom item")
        write_yaml(path, schema)

        def _do() -> None:
            _open_editor(path)
            print(f"\nSaved: {path}")
            print("Rename with 'r' if you want a different name.")

        app._run_command(_do, f"New {name}")
        self._refresh_items()
        app.notify(f"Created '{name}' — press 'r' to rename")


class TemplatesScreen(BrowserScreen):
    """Full-screen template browser."""

    def _browser_title(self) -> str:
        return "Templates"

    def _table_columns(self) -> list[str]:
        return ["Name", "Profile", "OS", "CPUs", "Memory", "Disk"]

    def _load_items(self) -> dict[str, Any]:
        return list_latita_templates()

    def _row_cells(self, name: str, data: dict[str, Any]) -> list[str]:
        return [
            name,
            str(data.get("profile", "-")),
            str(data.get("os_family", "-")),
            str(data.get("cpus", "-")),
            str(data.get("memory", "-")) if data.get("memory") != "-" else "-",
            str(data.get("disk_size", "-")),
        ]

    def _detail_for(self, name: str) -> Any:
        return self._items.get(name, {})

    def _is_builtin(self, name: str) -> bool:
        return is_builtin_template(name)

    def _get_path(self, name: str) -> Path:
        return get_template_path(name)

    def _file_ext(self) -> str:
        return ".latita"

    def _new_schema(self) -> dict[str, Any]:
        return {
            "profile": "headless",
            "description": "",
            "os_family": "fedora",
            "cpus": 2,
            "memory": 4096,
            "disk_size": "20G",
            "guest_user": "dev",
            "passwordless_sudo": True,
            "network": {
                "mode": "isolated",
                "nat_network": "",
                "mgmt_ip": "10.31.0.10",
                "mgmt_prefix": 24,
            },
            "ephemeral": {
                "transient": True,
                "destroy_on_stop": False,
            },
            "security": {
                "selinux": True,
                "no_guest_agent": True,
                "restrict_network": False,
                "allow_hosts": [],
            },
            "provision": {
                "packages": [],
                "write_files": [],
                "root_commands": [],
                "user_commands": [],
            },
        }

    def _copy_builtin(self, name: str, dst: Path) -> None:
        shutil.copy2(get_template_path(name), dst)

    def _user_dir(self) -> Path:
        return get_config().templates_dir


class CapsulesScreen(BrowserScreen):
    """Full-screen capsule browser."""

    def _browser_title(self) -> str:
        return "Capsules"

    def _table_columns(self) -> list[str]:
        return ["Name"]

    def _load_items(self) -> dict[str, Any]:
        return list_capsules()

    def _detail_for(self, name: str) -> Any:
        return self._items.get(name, {})

    def _is_builtin(self, name: str) -> bool:
        return is_builtin_capsule(name)

    def _get_path(self, name: str) -> Path:
        return get_capsule_path(name)

    def _file_ext(self) -> str:
        return ".cap"

    def _new_schema(self) -> dict[str, Any]:
        return {
            "description": "",
            "compatible_profiles": ["headless", "desktop"],
            "compatible_os": ["fedora", "ubuntu", "debian"],
            "live_commands": [],
            "provision": {
                "packages": [],
                "write_files": [],
                "root_commands": [],
                "user_commands": [],
            },
        }

    def _copy_builtin(self, name: str, dst: Path) -> None:
        shutil.copy2(get_capsule_path(name), dst)

    def _user_dir(self) -> Path:
        return get_config().capsules_dir


# ---------------------------------------------------------------------------
# Dashboard (main screen)
# ---------------------------------------------------------------------------

class Dashboard(App):
    """Minimalistic two-pane TUI dashboard."""

    CSS_PATH = Path(__file__).with_suffix(".tcss")

    BINDINGS = [
        Binding("tab", "toggle_pane", "Switch pane", show=False),
        Binding("q", "quit", "Quit", show=True),
        Binding("c", "create", "Create VM", show=False),
        Binding("r", "run", "Run one-shot", show=False),
        Binding("b", "bootstrap", "Bootstrap", show=False),
        Binding("d", "doctor", "Doctor", show=False),
        Binding("t", "templates", "Templates", show=True),
        Binding("p", "capsules", "Capsules", show=True),
        Binding("s", "start", "Start VM", show=False),
        Binding("S", "stop", "Stop VM", show=False),
        Binding("P", "pause", "Pause VM", show=False),
        Binding("M", "resume", "Resume VM", show=False),
        Binding("D", "destroy", "Destroy VM", show=False),
        Binding("h", "ssh", "SSH", show=False),
        Binding("k", "connect", "Connect", show=False),
        Binding("a", "apply_capsule", "Apply Capsule", show=False),
        Binding("i", "info", "Info", show=False),
        Binding("R", "refresh", "Refresh", show=False),
    ]

    selected_vm = reactive(None)

    def __init__(self) -> None:
        super().__init__()
        self._vm_list: list[dict[str, Any]] = []
        self._action_items: dict[str, ActionItem] = {}
        self._group_headers: dict[str, GroupHeader] = {}
        self._vm_init_cache: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left-pane"):
                yield Static("VMs", id="left-title")
                yield DataTable(id="vm-table", cursor_type="row")
            with Vertical(id="right-pane"):
                yield Static("Actions", id="right-title")
                yield ListView(id="action-list")
        yield Static(
            "c:Create  r:Run  s:Start  S:Stop  P:Pause  M:Resume  D:Destroy  h:SSH  k:Connect  a:Apply  t:Templates  p:Capsules  R:Refresh  q:Quit",
            id="hint-pane",
        )
        yield Static("", id="statusbar")

    def on_mount(self) -> None:
        table = self.query_one("#vm-table", DataTable)
        table.add_columns("Name", "Source", "Status", "Init", "IP", "Template", "CPUs", "Mem")
        self._build_action_list()
        self._refresh_vm_list()
        table.focus()
        self.set_interval(5.0, self._poll_init_states)

    def _build_action_list(self) -> None:
        action_list = self.query_one("#action-list", ListView)
        specs = [
            ("__group__", "Lifecycle"),
            ("start", "  ▶ Start"),
            ("pause", "  ⏸ Pause"),
            ("resume", "  ▶ Resume"),
            ("stop", "  ⏹ Stop"),
            ("destroy", "  🗑 Destroy"),
            ("__group__", "VM"),
            ("ssh", "  SSH"),
            ("connect", "  Connect"),
            ("apply_capsule", "  Apply Capsule"),
            ("info", "  ℹ Info"),
            ("logs", "  📋 Logs"),
            ("__group__", "General"),
            ("create", "  Create VM"),
            ("run", "  Run one-shot"),
            ("templates", "  Templates"),
            ("capsules", "  Capsules"),
            ("bootstrap", "  Bootstrap"),
            ("doctor", "  Doctor"),
            ("__group__", ""),
            ("quit", "  Quit"),
        ]
        for aid, label in specs:
            if aid == "__group__":
                item = GroupHeader(label)
                self._group_headers[label] = item
                item.disabled = True
                item.add_class("group-header")
            else:
                item = ActionItem(aid, label)
                self._action_items[aid] = item
            action_list.append(item)
        if action_list.children:
            action_list.index = 0

    def watch_selected_vm(self, vm: Optional[dict[str, Any]]) -> None:
        self._update_action_states()
        self._update_statusbar()

    def _update_action_states(self) -> None:
        vm = self.selected_vm
        status = vm.get("status", "") if vm else ""
        is_external = vm.get("source") == "external" if vm else False

        visible = {
            "start": bool(vm and status not in ("running", "paused", "downloading", "creating")),
            "pause": bool(vm and status == "running"),
            "resume": bool(vm and status == "paused"),
            "stop": bool(vm and status in ("running", "paused")),
            "destroy": bool(vm and not is_external and status not in ("downloading", "creating")),
            "ssh": bool(vm and status == "running" and not is_external),
            "connect": bool(vm and status == "running" and not is_external),
            "apply_capsule": bool(vm and status == "running" and not is_external),
            "info": bool(vm),
            "logs": bool(vm and status == "running" and not is_external),
            "create": True,
            "run": True,
            "bootstrap": True,
            "doctor": True,
            "templates": True,
            "capsules": True,
            "quit": True,
        }

        lifecycle_visible = any(visible.get(k) for k in ("start", "pause", "resume", "stop", "destroy"))
        vm_visible = any(visible.get(k) for k in ("ssh", "connect", "apply_capsule", "info", "logs"))

        for key, item in self._action_items.items():
            item.display = visible.get(key, True)
            item.disabled = not visible.get(key, True)

        if "Lifecycle" in self._group_headers:
            self._group_headers["Lifecycle"].display = lifecycle_visible
        if "VM" in self._group_headers:
            self._group_headers["VM"].display = vm_visible

    def _update_statusbar(self) -> None:
        vm = self.selected_vm
        name = vm["name"] if vm else "—"
        total = len(self._vm_list)
        cfg = get_config()
        mode_label = "Session" if cfg.is_session else "System"
        mode_hint = "isolated" if cfg.is_session else "shared"
        status = self.query_one("#statusbar", Static)
        status.update(f" sel: {name} | {total} VMs | Mode: {mode_label} ({mode_hint})")

    def _update_statusbar_msg(self, msg: str) -> None:
        status = self.query_one("#statusbar", Static)
        status.update(f" {msg}")

    # --- Unified runner ------------------------------------------------------

    def _run_command(self, fn: Callable[[], Any], label: str) -> Any:
        """Suspend TUI, run fn in the real terminal, then prompt to return."""
        self._pause_refresh()
        try:
            with self.suspend():
                from latita import ui as _ui
                from latita import operations as _ops
                from latita import capsules as _caps
                from latita import utils as _utils
                from latita import prompts as _prompts

                plain_console = Console(file=sys.__stdout__, color_system="auto", width=120)
                _modules = [_ui, _ops, _caps, _utils, _prompts]
                _old = {mod: getattr(mod, "console", None) for mod in _modules}
                for mod in _modules:
                    if _old[mod] is not None:
                        mod.console = plain_console

                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        try:
                            result = fn()
                        except KeyboardInterrupt:
                            print("\nCanceled.")
                            result = None
                        except Exception as exc:
                            print(f"\nError: {exc}")
                            result = None
                    print(f"\n[latita] {label} — Press Enter to return to menu...")
                    try:
                        input()
                    except (EOFError, KeyboardInterrupt):
                        pass
                finally:
                    for mod in _modules:
                        if _old[mod] is not None:
                            mod.console = _old[mod]
                return result
        finally:
            self._resume_refresh()

    # --- Focus switching -----------------------------------------------------

    def action_toggle_pane(self) -> None:
        vm_table = self.query_one("#vm-table", DataTable)
        action_list = self.query_one("#action-list", ListView)
        if self.focused is vm_table:
            self._ensure_valid_cursor(action_list)
            action_list.focus()
        else:
            vm_table.focus()

    def _ensure_valid_cursor(self, action_list: ListView) -> None:
        children = list(action_list.children)
        if not children:
            return
        idx = action_list.index if action_list.index is not None else 0
        if 0 <= idx < len(children):
            child = children[idx]
            if isinstance(child, (ActionItem, GroupHeader)) and child.display and not child.disabled:
                return
        for direction in (1, -1):
            search_idx = idx
            for _ in range(len(children)):
                search_idx = (search_idx + direction) % len(children)
                child = children[search_idx]
                if isinstance(child, ActionItem) and child.display and not child.disabled:
                    action_list.index = search_idx
                    return

    # --- Event handlers ------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = self.query_one("#vm-table", DataTable)
        cursor = table.cursor_row
        if isinstance(cursor, int) and 0 <= cursor < len(self._vm_list):
            self.selected_vm = self._vm_list[cursor]
        else:
            self.selected_vm = None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on VM table triggers SSH. Guard against firing from other screens."""
        if self.selected_vm:
            self.action_ssh()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, ActionItem) and item.action_id and item.display:
            self._run_action_id(item.action_id)

    # --- VM list refresh -----------------------------------------------------

    def _refresh_vm_list(self) -> None:
        new_list = scan_instances()
        table = self.query_one("#vm-table", DataTable)
        old_name = self.selected_vm["name"] if self.selected_vm else None  # type: ignore[index]

        table.clear()
        self._vm_list = new_list
        selected_row: Optional[int] = None
        for i, e in enumerate(new_list):
            if e.get("source") == "external":
                init_label = "external"
            elif e.get("status") != "running":
                init_label = "—"
            else:
                init_label = self._vm_init_cache.get(e["name"], "—")
            source_label = f"[{e.get('source', 'latita')}]"
            table.add_row(
                e.get("name", "?"),
                source_label,
                e.get("status", "?"),
                init_label,
                e.get("ip") or e.get("mgmt_ip") or "—",
                e.get("template") or "—",
                str(e.get("cpus") or "—"),
                str(e.get("memory") or "—"),
            )
            if old_name and e.get("name") == old_name:
                selected_row = i

        if selected_row is not None:
            table.move_cursor(row=selected_row)
            self.selected_vm = new_list[selected_row]
        elif new_list:
            cursor = table.cursor_row
            if isinstance(cursor, int) and 0 <= cursor < len(new_list):
                self.selected_vm = new_list[cursor]
            else:
                table.move_cursor(row=0)
                self.selected_vm = new_list[0]
        else:
            self.selected_vm = None

    async def _poll_init_states(self) -> None:
        """Background update of VM init states — never blocks the UI."""
        running_latita = [
            e for e in self._vm_list
            if e.get("status") == "running" and e.get("source") == "latita"
        ]
        if not running_latita:
            return

        async def _check_one(name: str) -> tuple[str, dict[str, str]]:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(get_vm_init_state, name),
                    timeout=15.0,
                )
                return name, result
            except Exception:
                return name, {"cloud_init": "n/a", "desktop": "n/a", "overall": "—"}

        results = await asyncio.gather(*[_check_one(e["name"]) for e in running_latita])
        changed = False
        for name, result in results:
            state = result.get("overall", "—")
            if state == "n/a":
                state = "—"
            prev = self._vm_init_cache.get(name)
            if prev != state:
                if prev == "initializing" and state == "ready":
                    self.notify(f"VM '{name}' is ready", severity="information")
                elif prev == "initializing" and state == "failed":
                    self.notify(f"VM '{name}' failed to initialize", severity="error")
                elif prev in ("—", None) and state == "initializing":
                    pass
                elif state == "ready" and prev in ("—", None):
                    pass
                self._vm_init_cache[name] = state
                changed = True
        # Prune stale entries
        current_names = {e["name"] for e in self._vm_list}
        stale = [n for n in self._vm_init_cache if n not in current_names]
        for n in stale:
            del self._vm_init_cache[n]
            changed = True
        if changed:
            self._refresh_vm_list()

    # --- Action dispatcher ---------------------------------------------------

    def _run_action_id(self, action_id: str) -> None:
        method = getattr(self, f"action_{action_id}", None)
        if method:
            method()

    def _trigger_refresh(self) -> None:
        """Refresh VM list after a state-changing action completes."""
        self._refresh_vm_list()

    # --- Unified runner ------------------------------------------------------

    def _run_command(self, fn: Callable[[], Any], label: str) -> dict[str, Any]:
        """Suspend TUI, run fn in the real terminal, then prompt to return.

        Returns a dict with ``ok`` (bool) and ``error`` (str or None).
        """
        error_msg: str | None = None
        with self.suspend():
            from latita import ui as _ui
            from latita import operations as _ops
            from latita import capsules as _caps
            from latita import utils as _utils
            from latita import prompts as _prompts

            plain_console = Console(file=sys.__stdout__, color_system="auto", width=120)
            _modules = [_ui, _ops, _caps, _utils, _prompts]
            _old = {mod: getattr(mod, "console", None) for mod in _modules}
            for mod in _modules:
                if _old[mod] is not None:
                    mod.console = plain_console

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    try:
                        fn()
                    except KeyboardInterrupt:
                        print("\nCanceled.")
                        error_msg = "Canceled"
                    except Exception as exc:
                        print(f"\nError: {exc}")
                        error_msg = str(exc)
                print(f"\n[latita] {label} — Press Enter to return to menu...")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
            finally:
                for mod in _modules:
                    if _old[mod] is not None:
                        mod.console = _old[mod]

        if error_msg:
            self.notify(f"{label} failed: {error_msg}", severity="error")
        else:
            self.notify(f"{label} completed", severity="information")
        return {"ok": error_msg is None, "error": error_msg}

    # --- Screen result callbacks ---------------------------------------------

    def _check_base_image(self, base_image: str) -> bool:
        """Return True if base image exists or was just downloaded."""
        cfg = get_config()
        base_img = cfg.base_dir / base_image
        if base_img.exists():
            return True
        # Is it in the catalog?
        in_catalog = any(v["filename"] == base_image for v in BASE_IMAGES.values())
        if not in_catalog:
            self.notify(f"Base image '{base_image}' not found and not in catalog", severity="error")
            return False
        return False  # Caller should prompt

    def _download_and_create(self, result: dict[str, Any], base_image: str | None = None) -> None:
        """Download base image if needed, then create/run in background."""
        recipe = result["recipe"]
        template_name = recipe.get("template_name", recipe.get("profile", "headless"))
        if base_image:
            recipe["base_image"] = base_image
        img = recipe.get("base_image", get_config().default_base_name)
        mode = result.get("mode", "create")
        command = recipe.get("command")
        vm_name = recipe.get("name", "vm")
        label = f"{'Create' if mode == 'create' else 'Run'} {vm_name}"
        cfg = get_config()

        needs_download = not (cfg.base_dir / img).exists()
        if needs_download:
            inst_dir = cfg.inst_dir / vm_name
            inst_dir.mkdir(parents=True, exist_ok=True)
            from .metadata import write_instance_recipe
            write_instance_recipe(vm_name, recipe)
            (inst_dir / ".downloading").write_text(img)
            self._trigger_refresh()
            self._update_statusbar_msg(f"Downloading {img} for {vm_name}...")

        def _do() -> None:
            try:
                if needs_download:
                    self.call_from_thread(lambda: self._update_statusbar_msg(f"Downloading {img} for {vm_name}..."))
                ok = _maybe_download_base(img)
                if not ok:
                    marker = cfg.inst_dir / vm_name / ".downloading"
                    if marker.exists():
                        marker.unlink()
                    self.call_from_thread(lambda: self._rollback_pending(vm_name))
                    self.call_from_thread(lambda: self.notify(f"Base image '{img}' not available", severity="error"))
                    return
                if needs_download:
                    marker = cfg.inst_dir / vm_name / ".downloading"
                    if marker.exists():
                        marker.unlink()
                    (cfg.inst_dir / vm_name / ".creating").write_text(img)
                    self.call_from_thread(lambda: self._update_statusbar_msg(f"Creating {vm_name}..."))
                    self.call_from_thread(self._trigger_refresh)
                if mode == "create":
                    create_instance(template_name, name=recipe.get("name"), overrides=recipe, wait=False)
                else:
                    if needs_download:
                        self.call_from_thread(lambda: self._rollback_pending(vm_name))
                    run_instance(
                        template_name,
                        command=command.split() if command else None,
                        overrides=recipe,
                    )
                creating_marker = cfg.inst_dir / vm_name / ".creating"
                if creating_marker.exists():
                    creating_marker.unlink()
                self.call_from_thread(lambda: self.notify(f"{label} started", severity="information"))
                self.call_from_thread(self._trigger_refresh)
            except Exception as exc:
                for m in (".downloading", ".creating"):
                    p = cfg.inst_dir / vm_name / m
                    if p.exists():
                        p.unlink()
                self.call_from_thread(lambda: self._rollback_pending(vm_name))
                self.call_from_thread(lambda: self.notify(f"{label} failed: {exc}", severity="error"))

        self.notify(f"{label} in background...", severity="information")
        threading.Thread(target=_do, daemon=True).start()

    def _rollback_pending(self, name: str) -> None:
        """Remove a pending (downloading/failed) instance directory."""
        from shutil import rmtree
        cfg = get_config()
        inst_dir = cfg.inst_dir / name
        if inst_dir.exists() and not (inst_dir / f"{name}.qcow2").exists():
            rmtree(inst_dir, ignore_errors=True)
        self._trigger_refresh()

    def _on_create_done(self, result: dict[str, Any] | None) -> None:
        if result is None:
            return
        recipe = result["recipe"]
        base_image = recipe.get("base_image", get_config().default_base_name)
        cfg = get_config()
        if (cfg.base_dir / base_image).exists():
            self._download_and_create(result)
            return
        in_catalog = any(v["filename"] == base_image for v in BASE_IMAGES.values())
        if not in_catalog:
            self.notify(f"Base image '{base_image}' not found and not in catalog", severity="error")
            return

        def _on_image_chosen(filename: str | None) -> None:
            if filename:
                self._download_and_create(result, base_image=filename)

        self.push_screen(BaseImagePickerScreen(base_image, _on_image_chosen))

    def _on_run_done(self, result: dict[str, Any] | None) -> None:
        if result is None:
            return
        recipe = result["recipe"]
        base_image = recipe.get("base_image", get_config().default_base_name)
        cfg = get_config()
        if (cfg.base_dir / base_image).exists():
            self._download_and_create(result)
            return
        in_catalog = any(v["filename"] == base_image for v in BASE_IMAGES.values())
        if not in_catalog:
            self.notify(f"Base image '{base_image}' not found and not in catalog", severity="error")
            return

        def _on_image_chosen(filename: str | None) -> None:
            if filename:
                self._download_and_create(result, base_image=filename)

        self.push_screen(BaseImagePickerScreen(base_image, _on_image_chosen))

    def _on_capsule_chosen(self, capsule_name: str | None) -> None:
        if capsule_name is None:
            return
        name = self._selected_name()
        if not name:
            return
        self._update_statusbar_msg(f"Applying {capsule_name} to {name}...")

        def _apply() -> None:
            try:
                _ensure_running(name)
                apply_capsule_live(name, capsule_name)
                self.call_from_thread(lambda: self.notify(f"Applied {capsule_name} to {name}", severity="information"))
            except Exception as exc:
                self.call_from_thread(lambda: self.notify(f"Apply failed: {exc}", severity="error"))
            finally:
                self.call_from_thread(lambda: self._update_statusbar())

        threading.Thread(target=_apply, daemon=True).start()

    # --- Global actions ------------------------------------------------------

    def action_quit(self) -> None:
        self.exit()

    def action_refresh(self) -> None:
        self._trigger_refresh()

    def action_create(self) -> None:
        self.push_screen(CreateVMScreen(), self._on_create_done)

    def action_run(self) -> None:
        self.push_screen(RunVMScreen(), self._on_run_done)

    def action_bootstrap(self) -> None:
        def _do() -> None:
            try:
                bootstrap_host()
            except Exception as exc:
                print(f"Bootstrap failed: {exc}")
        self._run_command(_do, "Bootstrap")

    def action_doctor(self) -> None:
        def _do() -> None:
            try:
                doctor()
            except Exception as exc:
                print(f"Doctor failed: {exc}")
        self._run_command(_do, "Doctor")

    def action_templates(self) -> None:
        self.push_screen(TemplatesScreen())

    def action_capsules(self) -> None:
        self.push_screen(CapsulesScreen())

    # --- VM actions ----------------------------------------------------------

    def _run_vm_action(self, name: str, fn: Callable[[], Any], label: str) -> None:
        """Run a VM action in a background thread with toast notifications."""
        self._update_statusbar_msg(f"{label}...")
        
        def _do() -> None:
            try:
                fn()
                self.call_from_thread(lambda: self.notify(f"{label} completed", severity="information"))
                self.call_from_thread(self._trigger_refresh)
            except Exception as exc:
                self.call_from_thread(lambda: self.notify(f"{label} failed: {exc}", severity="error"))

        threading.Thread(target=_do, daemon=True).start()

    def action_start(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return
        self._run_vm_action(name, lambda: start_instance(name, wait=False), f"Start {name}")

    def action_stop(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return
        self._run_vm_action(name, lambda: stop_instance(name), f"Stop {name}")

    def action_pause(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return
        self._run_vm_action(name, lambda: pause_instance(name), f"Pause {name}")

    def action_resume(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return
        self._run_vm_action(name, lambda: resume_instance(name), f"Resume {name}")

    def action_destroy(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return

        def _on_result(confirmed: bool) -> None:
            if confirmed:
                self._run_vm_action(name, lambda: destroy_instance(name), f"Destroy {name}")

        self.push_screen(TypeToConfirmScreen(f"Destroy VM '{name}' and shred its disk?", "destroy", _on_result))

    def action_ssh(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return
        _ensure_running(name)
        self._run_command(lambda: ssh_instance(name), f"SSH {name}")

    def action_connect(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return
        _ensure_running(name)

        # Use stored libvirt URI (matches how the VM was created)
        from .metadata import read_instance_env, read_instance_spec
        env = read_instance_env(name)
        uri = env.get("LIBVIRT_URI") or get_config().libvirt_uri

        # Check virt-viewer is installed
        if not shutil.which("virt-viewer"):
            self.notify("virt-viewer is not installed", severity="warning")
            self._run_command(lambda: ssh_instance(name), f"Connect {name}")
            return

        # Verify domain exists at this URI before launching viewer
        check = subprocess.run(
            ["virsh", "-c", uri, "domstate", name],
            capture_output=True, text=True, timeout=5,
        )
        if check.returncode != 0:
            self.notify(f"Cannot find '{name}' at {uri}. Try SSH instead.", severity="error")
            return

        spec = read_instance_spec(name)
        mode = "SPICE" if spec and spec.get("graphics") == "spice" else "serial console"

        # Launch virt-viewer and capture stderr to detect immediate failures
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".stderr", delete=False) as tf:
            stderr_path = tf.name
        proc = subprocess.Popen(
            ["virt-viewer", "--connect", uri, "--wait", name],
            stdout=subprocess.DEVNULL,
            stderr=open(stderr_path, "w"),
            start_new_session=True,
        )

        # Poll briefly; if it exits quickly, something went wrong
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass  # Still running — good
        else:
            stderr_text = Path(stderr_path).read_text().strip()
            Path(stderr_path).unlink(missing_ok=True)
            if proc.returncode != 0:
                self.notify(f"virt-viewer failed: {stderr_text or 'unknown error'}", severity="error")
                return

        Path(stderr_path).unlink(missing_ok=True)
        self.notify(f"Launched virt-viewer ({mode}) for {name}")

    def action_apply_capsule(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return
        self.push_screen(ApplyCapsuleScreen(name), self._on_capsule_chosen)

    def action_info(self) -> None:
        vm = self.selected_vm
        if not vm:
            self.notify("Select a VM first", severity="warning")
            return
        self.push_screen(InfoScreen(vm))

    def action_logs(self) -> None:
        name = self._selected_name()
        if not name:
            self.notify("Select a VM first", severity="warning")
            return
        self._update_statusbar_msg(f"Fetching logs for {name}...")

        def _fetch() -> None:
            log = fetch_vm_error_log(name)
            self.call_from_thread(lambda: self._show_logs(name, log))

        threading.Thread(target=_fetch, daemon=True).start()

    def _show_logs(self, name: str, log: str) -> None:
        self._update_statusbar()
        self.push_screen(LogsScreen(name, log))

    def _selected_name(self) -> str | None:
        vm = self.selected_vm
        return vm["name"] if vm else None
