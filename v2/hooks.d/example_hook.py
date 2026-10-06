"""Example user hook. Every .py file in hooks.d/ is loaded at start-up.

This one adds a footer reminding people that leave answers are drafts until recorded.
Delete or edit freely.
"""

from hrai.hooks import hook


@hook("post_response", order=90)
def leave_footer(ctx):
    if ctx.get("agent") == "leave" and "route_to_manager" in ctx.get("result", ""):
        ctx["result"] += "\n\n(Sent to the manager's approval queue; nothing is final until they decide.)"
