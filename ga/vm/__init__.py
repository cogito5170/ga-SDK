"""``ga vm`` (CMD-OPS1): GA Engine on a VM as user-level systemd services that survive reboots and rebuild from git.

    ga vm check [--home H] [--min-free-gb N] [--disk-only]     one verdict JSON; exit 1 when not ready
    ga vm install [--home H] [--dry-run]                       idempotent; never runs sudo, never reads a credential file
    ga vm status | enable-hub | uninstall

The installer prints the one sudo line (lingering) for the user to run. All state lives in git; see docs/VM.md.
"""
