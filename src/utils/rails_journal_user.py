"""Attribution of migration-created journals to a real OpenProject user.

A Rails console session — and a ``bundle exec rails runner`` process — starts
with nothing having set ``User.current``. OpenProject's ``User.current`` does
not return ``nil`` in that state: it returns ``User.anonymous``. Every
``wp.save!`` the migration performs therefore records its journal against the
Anonymous user, which is how migrated work packages ended up with two thirds of
their activity attributed to nobody.

This module produces the Ruby needed to fix that at the two places a script can
enter OpenProject:

* :func:`console_command` — a single line sent once per tmux console session.
  ``User.current`` lives in ``RequestStore``, a thread-local that no middleware
  clears in a console, so one assignment covers every subsequent command,
  including the ``load '/tmp/j2o_*.rb'`` path that runs script files inside the
  console process.
* :func:`script_preamble` — the same assignment prepended to script files that
  are handed to ``bundle exec rails runner``. Those run in a *separate*
  process, so the console-session assignment does not reach them.

Deliberately NOT done here: wrapping scripts in ``User.execute_as(user) { ... }``.
A block wrap turns every script into multi-line input for IRB, and unterminated
multi-line input is exactly what wedged the console before (see
``RailsConsoleClient._stabilize_console``). A plain assignment carries no such
risk.

Also deliberately not done: ``Journal::NotificationConfiguration.with(false)``.
It is block-scoped, so it would need the same wrap — and the mail is delivered
by a separate worker process anyway, which a console-side toggle would not
reach.
"""

from __future__ import annotations

from src import config

# Ruby local used to hold the resolved user. Prefixed to make collisions with a
# surrounding script's own locals implausible.
_VAR = "__j2o_journal_user"


def _configured_user() -> str:
    """Return the configured journal user (login or numeric id), or ``""``."""
    try:
        raw = config.migration_config.get("journal_user") or ""
    except Exception:
        # Config not bootstrapped (e.g. unit tests touching the Ruby helpers
        # directly). Fall back to the unconfigured default rather than raising
        # out of a code path whose only job is attribution.
        return ""
    return str(raw).strip()


def lookup_expression() -> str:
    """Return a Ruby expression evaluating to the journal user, or ``nil``.

    A purely numeric setting is treated as a user id, anything else as a login.
    The login is emitted as a single-quoted Ruby literal via
    ``escape_ruby_single_quoted``: double-quoted literals interpolate
    ``#{...}``, and this value comes from configuration that may itself have
    been filled from a less trusted place.
    """
    # Imported inside the function on purpose: keeping this module free of
    # import-time infrastructure dependencies means any layer can import it
    # without closing a cycle through ``openproject_client``.
    from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

    configured = _configured_user()
    if not configured:
        return "nil"
    if configured.isdigit():
        return f"User.find_by(id: {int(configured)})"
    return f"User.find_by(login: '{escape_ruby_single_quoted(configured)}')"


def _assignment(*, quiet: bool) -> str:
    """Build the one-line assignment shared by both entry points."""
    # ``User.system`` rather than ``User.anonymous`` as the unconfigured
    # default: it is a builtin that does not show up in user administration and
    # renders as "System", so unattributed bookkeeping writes read as machine
    # actions instead of as an unknown person.
    report = "" if quiet else f'; puts "J2O journal user: #{{{_VAR}.id}} #{{{_VAR}.name}}"'
    return (
        f"begin; {_VAR} = ({lookup_expression()}) || User.system; "
        f"User.current = {_VAR} if {_VAR}{report}; "
        'rescue => e; puts "J2O journal user error: #{e.class}: #{e.message}"; end'
    )


def console_command() -> str:
    """Return the one-liner to send once per Rails console session.

    Single line on purpose: multi-line input is what the console is fragile
    about.
    """
    return _assignment(quiet=False)


def script_preamble() -> str:
    """Return the preamble to prepend to a script file run via ``rails runner``.

    Ends with a newline so it composes with any script body, and stays quiet so
    it cannot pollute stdout that a caller parses for JSON markers.
    """
    return _assignment(quiet=True) + "\n"


def prepend_to_script(ruby_script: str) -> str:
    """Return ``ruby_script`` with the journal-user preamble in front.

    Idempotent: a script that already carries the preamble is returned
    unchanged, so it is safe to apply on a retry path that may have already
    passed through here.
    """
    if _VAR in ruby_script:
        return ruby_script
    return script_preamble() + ruby_script
