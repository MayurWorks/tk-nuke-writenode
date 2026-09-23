"""Build REAL tk-core Template objects from the config repo's templates.yml
(includes resolved), so tests exercise genuine apply_fields/get_fields.

Needs a tk-core checkout (TK_CORE_PYTHON) and the config repo's core/ folder
(NFA_CFG_CORE); test_real_templates.py skips itself when either is missing."""
import sys, os, yaml
sys.path.insert(0, os.environ.get("TK_CORE_PYTHON", "/home/claude/tk-core/python"))
from tank import template as tk_template, templatekey

CFG = os.environ.get("NFA_CFG_CORE", "/home/claude/nfa-shotgun-configuration/core")

def _merge(dst, src):
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v

def load_data():
    top = yaml.safe_load(open(os.path.join(CFG, "templates.yml")))
    data = {}
    for inc in top.get("includes", []):
        _merge(data, yaml.safe_load(open(os.path.normpath(os.path.join(CFG, inc)))))
    _merge(data, {k: v for k, v in top.items() if k != "includes"})
    return data

def load_templates():
    data = load_data()
    # resolve @aliases inside path definitions
    paths = data["paths"]
    for _ in range(10):
        for name, d in paths.items():
            defn = d if isinstance(d, str) else d["definition"]
            while "@" in defn:
                import re
                m = re.search(r"@(\w+)", defn)
                ref = paths[m.group(1)]
                rdef = ref if isinstance(ref, str) else ref["definition"]
                defn = defn.replace(m.group(0), rdef)
            if isinstance(d, str):
                paths[name] = defn
            else:
                d["definition"] = defn
    keys = templatekey.make_keys(data["keys"])
    roots = {"primary": {sys.platform: "/jobs/SlateX"}}
    return tk_template.make_template_paths(paths, keys, roots, default_root="primary")

if __name__ == "__main__":
    t = load_templates()
    for n in ("nuke_shot_work", "nuke_shot_render", "nuke_shot_write_movie", "nuke_shot_render_pub"):
        print(n, "->", t[n].definition)
