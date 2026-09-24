"""The owner console's projection of a plugin manifest.

The console generates its capability editor, approval summaries, denies
editor and hidden-resource picker from the manifest (manifests are data:
adding a plugin must not touch the console either). `plugins_admin.view()`
carries this projection as `manifest`, so one `GET /v1/admin/plugins`
gives the console everything it renders.

Why a projection and not the raw manifest: the console needs the lattice
vocabulary, already sorted into the two places a capability stores it.
Set forms (list, subtree, pattern) are *selectors*; scalar forms (range,
flag, level) are *constraints*, whether the manifest declared them as a
narrowing or as a constraint (`authority/capability.target_forms` makes the
same split). Derived narrowings are dropped because no grant can set them.
Params schemas and skill text stay out: the console never builds a call.
"""

from __future__ import annotations

from .manifest import SCALAR_FORMS, SET_FORMS, Manifest


def admin_view(m: Manifest) -> dict:
    selectors, constraints = [], []
    for n in m.narrowings:
        if n.derived_from:
            continue
        entry = {"name": n.dimension, "form": n.form, "resource": n.resource,
                 "applies_to": list(n.applies_to), "enforcement": n.enforcement,
                 "doc": n.doc}
        if n.form in SET_FORMS:
            selectors.append(entry)
        elif n.form in SCALAR_FORMS:
            constraints.append({**entry, "values": n.values, "default": None})
    for c in m.constraints:
        constraints.append({"name": c.name, "form": c.form, "resource": None,
                            "applies_to": list(c.applies_to), "enforcement": c.enforcement,
                            "doc": c.doc, "values": c.values, "default": c.default})
    return {
        "actions": [{"name": a.name, "side_effect": a.side_effect,
                     "modes": list(a.effective_modes), "resource": a.resource,
                     "selector_param": a.selector_param, "schedulable": a.schedulable,
                     "summary_template": a.summary_template, "doc": a.doc}
                    for a in m.actions],
        "resources": {kind: {"display": r.display, "resolve": r.resolve,
                             "hideable": r.hideable, "id_format": r.id_format}
                      for kind, r in m.resources.items()},
        "selectors": selectors,
        "constraints": constraints,
    }
