"""MCP Foundry — discovery engine.

Scans a code repo and returns its capabilities per CONTRACT.md.

Pure Python, stdlib only. Uses the `ast` module to parse the target code; it never
imports the target. Per-file parsing is wrapped in try/except so a single bad file
never crashes the whole scan.

Public API:
    discover(repo_path: str) -> dict
        {
          'project': {'language': str, 'frameworks': [str], 'root': str},
          'capabilities': [Capability, ...],
          'excluded': [{'name': str, 'reason': str}, ...]
        }
"""
from __future__ import annotations

import ast
import os
import re
import warnings

# ---------------------------------------------------------------------------
# Type mapping (Python annotation -> contract param type)
# ---------------------------------------------------------------------------
# Contract: str->string, int->integer, float->number, bool->boolean,
#           list->array, dict->object, missing->unknown.
_TYPE_MAP = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "List": "array",
    "tuple": "array",
    "Tuple": "array",
    "set": "array",
    "Set": "array",
    "dict": "object",
    "Dict": "object",
    "Mapping": "object",
    "bytes": "string",
    "Decimal": "number",
}

_RETURN_TYPE_MAP = {
    "str": "str",
    "int": "int",
    "float": "int",  # rough shape bucket
    "bool": "bool",
    "list": "list",
    "List": "list",
    "tuple": "list",
    "Tuple": "list",
    "set": "list",
    "Set": "list",
    "dict": "dict",
    "Dict": "dict",
    "Mapping": "dict",
    "None": "none",
}

# Directories we never descend into for capability discovery.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".tox", ".mypy_cache", ".pytest_cache", "build",
    "dist", ".eggs", "site-packages", ".idea", ".vscode",
}

# Path fragments that mark a file as test/migration/fixture (excluded surface).
_EXCLUDE_PATH_PARTS = ("tests", "test", "migrations", "fixtures")

# Framework objects whose presence as a positional/required param means the
# function takes an opaque framework object rather than plain data.
_OPAQUE_PARAM_NAMES = {"request", "req", "self", "cls", "session", "db", "context", "ctx"}


# ---------------------------------------------------------------------------
# Risk classification (CONTRACT.md heuristics)
# ---------------------------------------------------------------------------
def _classify_risk(name: str, http_method: str | None) -> str:
    """Return one of read|write|destructive|sensitive.

    Order of precedence (most specific / most dangerous wins where it applies):
      - sensitive: name mentions key/secret/password/token/ssh
      - destructive: DELETE method, or delete_/drop_/purge_/remove_ verbs
      - write: POST/PUT/PATCH, or create_/update_/add_/submit_/change_ verbs
      - read: GET, or list_/get_/fetch_/verify_ verbs
    Default (nothing matches): "write" (classify up when unsure).
    """
    lname = (name or "").lower()
    method = (http_method or "").upper()

    # Sensitive — anything touching credentials.
    if re.search(r"(key|secret|password|passwd|token|ssh)", lname):
        return "sensitive"

    # Destructive.
    if method == "DELETE":
        return "destructive"
    if re.search(r"(^|_)(delete|drop|purge|remove)", lname):
        return "destructive"

    # Write.
    if method in ("POST", "PUT", "PATCH"):
        return "write"
    if re.search(r"(^|_)(create|update|add|submit|change|set|run|cancel|reset|transfer|replace|rename|backup)", lname):
        return "write"

    # Read.
    if method in ("GET", "HEAD", "OPTIONS"):
        return "read"
    if re.search(r"(^|_)(list|get|fetch|verify|read|show|find|search|status|version|info|health|count)", lname):
        return "read"

    # Unknown -> classify up.
    return "write"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------
def _annotation_to_param_type(annotation: ast.AST | None) -> str:
    """Map a Python annotation AST node to a contract param type."""
    if annotation is None:
        return "unknown"
    base = _base_annotation_name(annotation)
    return _TYPE_MAP.get(base, "unknown")


def _annotation_to_return_shape(annotation: ast.AST | None) -> str:
    """Map a return annotation AST node to a rough returns shape."""
    if annotation is None:
        return "unknown"
    base = _base_annotation_name(annotation)
    return _RETURN_TYPE_MAP.get(base, "unknown")


def _base_annotation_name(annotation: ast.AST | None) -> str:
    """Best-effort extract the base name of an annotation.

    Handles: Name (`int`), Attribute (`typing.List` -> `List`),
    Subscript (`list[str]` / `Optional[int]` -> unwrap), and
    `X | None` unions (-> the non-None member).
    """
    if annotation is None:
        return ""
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Constant):
        if annotation.value is None:
            return "None"
        return type(annotation.value).__name__
    if isinstance(annotation, ast.Subscript):
        # Optional[X] / Union[X, None] -> look inside; else use the container.
        outer = _base_annotation_name(annotation.value)
        if outer in ("Optional",):
            return _base_annotation_name(annotation.slice)
        if outer in ("Union",):
            # first non-None member
            slc = annotation.slice
            elts = slc.elts if isinstance(slc, ast.Tuple) else [slc]
            for e in elts:
                nm = _base_annotation_name(e)
                if nm != "None":
                    return nm
            return "None"
        return outer
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        # X | None  /  X | Y  -> first non-None member
        left = _base_annotation_name(annotation.left)
        right = _base_annotation_name(annotation.right)
        if left and left != "None":
            return left
        return right
    return ""


def _literal_default(node: ast.AST | None):
    """Return a JSON-serializable default value from a default-value AST node."""
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:
        # Non-literal default (e.g. a call or name) — represent as None.
        return None


def _get_docstring_summary(node: ast.AST) -> str:
    """First non-empty line of the function/class docstring, else ''."""
    doc = ast.get_docstring(node) or ""
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _returns_shape_from_body(node: ast.FunctionDef) -> str:
    """Heuristically infer a return shape by scanning `return` statements."""
    shapes = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return):
            val = sub.value
            if val is None or (isinstance(val, ast.Constant) and val.value is None):
                shapes.add("none")
            elif isinstance(val, (ast.List, ast.ListComp)):
                shapes.add("list")
            elif isinstance(val, (ast.Dict, ast.DictComp)):
                shapes.add("dict")
            elif isinstance(val, ast.Constant):
                v = val.value
                if isinstance(v, bool):
                    shapes.add("bool")
                elif isinstance(v, int):
                    shapes.add("int")
                elif isinstance(v, str):
                    shapes.add("str")
                else:
                    shapes.add("unknown")
            else:
                shapes.add("unknown")
    if len(shapes) == 1:
        return shapes.pop()
    return "unknown"


# ---------------------------------------------------------------------------
# Param extraction
# ---------------------------------------------------------------------------
def _params_from_signature(func: ast.FunctionDef, skip_names: set[str] | None = None) -> list[dict]:
    """Build the contract `params` list from a function signature.

    `skip_names` — parameter names to drop (e.g. `request`, `self`).
    """
    skip = skip_names or set()
    params: list[dict] = []
    args = func.args

    posonly = list(getattr(args, "posonlyargs", []))
    positional = posonly + list(args.args)
    defaults = list(args.defaults)
    # Align defaults to the *trailing* positional args.
    num_with_default = len(defaults)
    default_offset = len(positional) - num_with_default

    for i, a in enumerate(positional):
        if a.arg in skip:
            continue
        default_node = None
        required = True
        if i >= default_offset:
            default_node = defaults[i - default_offset]
            required = False
        params.append(_make_param(a.arg, a.annotation, required, default_node))

    # keyword-only args
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if a.arg in skip:
            continue
        required = d is None
        params.append(_make_param(a.arg, a.annotation, required, d))

    return params


def _make_param(name: str, annotation, required: bool, default_node) -> dict:
    return {
        "name": name,
        "type": _annotation_to_param_type(annotation),
        "required": bool(required),
        "default": _literal_default(default_node),
        "description": "",
    }


# ---------------------------------------------------------------------------
# FastAPI / Flask decorator inspection
# ---------------------------------------------------------------------------
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _decorator_call_attr(dec: ast.AST):
    """If decorator is a call like `@app.get(...)` / `@router.post(...)` or
    `@app.route(...)`, return (object_name, attr_name, call_node). Else None."""
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
        attr = dec.func.attr
        obj = dec.func.value
        obj_name = obj.id if isinstance(obj, ast.Name) else (
            obj.attr if isinstance(obj, ast.Attribute) else ""
        )
        return obj_name, attr, dec
    return None


def _fastapi_route(dec: ast.AST):
    """Detect a FastAPI/Starlette-style route decorator.

    Returns {'method', 'path'} or None.
    Matches `@app.get("/x")`, `@router.post("/x")`, `@app.api_route("/x", methods=[...])`.
    """
    info = _decorator_call_attr(dec)
    if not info:
        return None
    obj_name, attr, call = info
    if attr in _HTTP_METHODS:
        return {"method": attr.upper(), "path": _first_str_arg(call)}
    if attr == "api_route":
        method = _methods_kwarg(call)
        return {"method": method, "path": _first_str_arg(call)}
    return None


def _flask_route(dec: ast.AST):
    """Detect a Flask route decorator `@app.route("/x", methods=["POST"])` /
    `@bp.route(...)`. Returns {'method', 'path'} or None."""
    info = _decorator_call_attr(dec)
    if not info:
        return None
    obj_name, attr, call = info
    if attr == "route":
        return {"method": _methods_kwarg(call) or "GET", "path": _first_str_arg(call)}
    return None


def _first_str_arg(call: ast.Call) -> str:
    for a in call.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
    return ""


def _methods_kwarg(call: ast.Call) -> str:
    """Read methods=[...] kwarg, return first method uppercased, else 'GET'."""
    for kw in call.keywords:
        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
            for e in kw.value.elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str):
                    return e.value.upper()
    return "GET"


def _flask_add_url_rule(stmt: ast.AST):
    """Detect `app.add_url_rule('/x', 'name', view_func, methods=[...])`.
    Returns {'path', 'view', 'method'} or None."""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return None
    call = stmt.value
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "add_url_rule"):
        return None
    path = _first_str_arg(call)
    view = None
    # view_func may be positional (3rd) or kwarg
    for kw in call.keywords:
        if kw.arg == "view_func" and isinstance(kw.value, ast.Name):
            view = kw.value.id
    if view is None:
        names = [a for a in call.args if isinstance(a, ast.Name)]
        if names:
            view = names[0].id
    return {"path": path, "view": view, "method": _methods_kwarg(call)}


# ---------------------------------------------------------------------------
# Pydantic body model field extraction (FastAPI)
# ---------------------------------------------------------------------------
def _collect_pydantic_models(tree: ast.Module) -> dict[str, list[dict]]:
    """Map class name -> [param dicts] for classes that look like Pydantic models.

    A class is treated as a model if it subclasses BaseModel (by name) or has
    only annotated class-level fields. Best-effort.
    """
    models: dict[str, list[dict]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {_base_annotation_name(b) for b in node.bases}
        looks_model = "BaseModel" in base_names
        fields: list[dict] = []
        annotated_only = True
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fname = stmt.target.id
                ptype = _annotation_to_param_type(stmt.annotation)
                required = stmt.value is None
                default = None
                if stmt.value is not None and not _is_pydantic_field_call(stmt.value):
                    default = _literal_default(stmt.value)
                fields.append({
                    "name": fname,
                    "type": ptype,
                    "required": required,
                    "default": default,
                    "description": _pydantic_field_description(stmt.value),
                })
            elif isinstance(stmt, (ast.Expr, ast.Pass)):
                continue
            elif isinstance(stmt, ast.FunctionDef):
                continue
            else:
                annotated_only = False
        if (looks_model or annotated_only) and fields:
            models[node.name] = fields
    return models


def _is_pydantic_field_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Field"
    )


def _pydantic_field_description(node: ast.AST | None) -> str:
    """Pull description="..." out of a Field(...) default, else ''."""
    if not _is_pydantic_field_call(node):
        return ""
    for kw in node.keywords:  # type: ignore[union-attr]
        if kw.arg == "description" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, str):
                return kw.value.value
    return ""


# ---------------------------------------------------------------------------
# Per-file scanning
# ---------------------------------------------------------------------------
def _slugify_name(name: str) -> str:
    """snake_case-ish slug from an identifier (keep it readable & valid).

    Keeps acronym runs together: "fetchSSHkey" -> "fetch_ssh_key",
    "runAWSBackups" -> "run_aws_backups", "changePackageAPI" -> "change_package_api".
    """
    # Insert _ between an acronym run and a following CamelCase word: SSHkey -> SSH_key
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    # Insert _ between a lowercase/digit and an uppercase: changePackage -> change_Package
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_").lower()
    return s or name.lower()


def _read_source(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        try:
            with open(path, "r", encoding="latin-1") as fh:
                return fh.read()
        except Exception:
            return None


def _safe_parse(src: str, filename: str):
    """ast.parse with SyntaxWarnings (e.g. invalid escape sequences in regex
    string literals) silenced. Returns the module, or raises like ast.parse."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(src, filename=filename)


def _is_excluded_path(rel_path: str) -> bool:
    parts = re.split(r"[/\\]", rel_path.lower())
    for p in parts[:-1]:  # directory components
        if p in _EXCLUDE_PATH_PARTS:
            return True
    fname = parts[-1]
    base = fname[:-3] if fname.endswith(".py") else fname
    if base.startswith("test_") or base.endswith("_test") or base == "tests" or base.startswith("tests"):
        return True
    if base.startswith("conftest"):
        return True
    return False


def _scan_python_file(abs_path: str, rel_path: str, capabilities: list, excluded: list,
                      frameworks: set, routed_symbols: set | None = None) -> None:
    """Parse one .py file and append discovered capabilities / exclusions.

    `routed_symbols` — function names already emitted as HTTP caps elsewhere
    (e.g. Django views wired via urls.py); we won't re-emit or noisily exclude them.
    """
    routed_symbols = routed_symbols or set()
    src = _read_source(abs_path)
    if src is None:
        excluded.append({"name": rel_path, "reason": "could not read file"})
        return
    try:
        tree = _safe_parse(src, abs_path)
    except (SyntaxError, ValueError):
        excluded.append({"name": rel_path, "reason": "unparseable (syntax error)"})
        return

    excluded_file = _is_excluded_path(rel_path)

    # Detect frameworks imported in this file.
    imported = _imported_names(tree)
    file_is_fastapi = bool(imported & {"fastapi", "FastAPI", "APIRouter"})
    file_is_flask = bool(imported & {"flask", "Flask", "Blueprint"})
    file_is_django = bool(imported & {"django"}) or rel_path.endswith("views.py")

    if file_is_fastapi:
        frameworks.add("fastapi")
    if file_is_flask:
        frameworks.add("flask")

    models = _collect_pydantic_models(tree)

    # Line lookup not needed beyond lineno; ast nodes carry .lineno.
    top_level_funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]

    routed_funcs: set[int] = set()  # id() of func nodes handled as a route

    for func in top_level_funcs:
        loc = f"{rel_path}:{func.lineno}"
        http = None
        framework = None
        skip_params = {"self", "cls"}

        # --- web route decorators ---
        for dec in func.decorator_list:
            fa = _fastapi_route(dec)
            if fa and fa.get("path") is not None:
                http = fa
                framework = "fastapi"
                frameworks.add("fastapi")
                break
            fl = _flask_route(dec)
            if fl and fl.get("path") is not None:
                http = fl
                framework = "flask"
                frameworks.add("flask")
                break

        if http is not None:
            routed_funcs.add(id(func))
            # FastAPI/Flask route function -> build params from signature + body model.
            params = _build_route_params(func, models, framework)
            cap = _make_capability(
                name=func.name,
                summary=_get_docstring_summary(func) or _humanize(func.name),
                params=params,
                returns=_func_returns(func),
                location=loc,
                framework=framework,
                http={"method": http["method"], "path": http["path"]},
                invocation="http",
                source_symbol=func.name,
            )
            capabilities.append(cap)
            continue

    # --- Flask add_url_rule registrations ---
    for stmt in tree.body:
        rule = _flask_add_url_rule(stmt)
        if rule and rule.get("view"):
            frameworks.add("flask")
            # find the view function in this module
            vfunc = next((f for f in top_level_funcs if f.name == rule["view"]), None)
            lineno = vfunc.lineno if vfunc else getattr(stmt, "lineno", 0)
            if vfunc:
                routed_funcs.add(id(vfunc))
            params = _build_route_params(vfunc, models, "flask") if vfunc else []
            capabilities.append(_make_capability(
                name=rule["view"],
                summary=_get_docstring_summary(vfunc) if vfunc else "",
                params=params,
                returns=_func_returns(vfunc) if vfunc else "unknown",
                location=f"{rel_path}:{lineno}",
                framework="flask",
                http={"method": rule["method"], "path": rule["path"]},
                invocation="http",
                source_symbol=rule["view"],
            ))

    # --- plain-python public top-level functions ---
    # Only when the file is NOT excluded and the function wasn't a route.
    if excluded_file:
        # record once that this file's surface is excluded (avoid noise per-func)
        return

    explicit_all = _module_all(tree)

    for func in top_level_funcs:
        if id(func) in routed_funcs:
            continue
        name = func.name
        # dunders / private
        if name.startswith("__") and name.endswith("__"):
            excluded.append({"name": f"{rel_path}::{name}", "reason": "dunder method"})
            continue
        if name.startswith("_"):
            excluded.append({"name": f"{rel_path}::{name}", "reason": "private (underscore-prefixed)"})
            continue
        # If __all__ is declared, honor it as the public API statement.
        if explicit_all is not None and name not in explicit_all:
            excluded.append({"name": f"{rel_path}::{name}", "reason": "not in __all__"})
            continue

        params = _params_from_signature(func, skip_names={"self", "cls"})

        # Already emitted as an HTTP cap (e.g. a Django view routed via urls.py).
        if name in routed_symbols:
            continue

        # Django-style view: takes `request` (opaque object). If it wasn't routed
        # via urls.py we can't proxy it cleanly, so exclude it from the surface.
        first_arg = func.args.args[0].arg if func.args.args else None
        if first_arg in _OPAQUE_PARAM_NAMES and not file_is_fastapi and not file_is_flask:
            excluded.append({
                "name": f"{rel_path}::{name}",
                "reason": f"takes opaque framework object '{first_arg}' (not routed via urls.py)",
            })
            continue

        cap = _make_capability(
            name=name,
            summary=_get_docstring_summary(func) or _humanize(name),
            params=params,
            returns=_func_returns(func),
            location=f"{rel_path}:{func.lineno}",
            framework="plain-python",
            http=None,
            invocation="in_process",
            source_symbol=name,
        )
        capabilities.append(cap)


def _build_route_params(func: ast.FunctionDef | None, models: dict, framework: str | None) -> list[dict]:
    """For a FastAPI/Flask route, build params from path/query params and an
    expanded Pydantic body model if a parameter is annotated with one."""
    if func is None:
        return []
    params: list[dict] = []
    args = func.args
    positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
    defaults = list(args.defaults)
    default_offset = len(positional) - len(defaults)

    for i, a in enumerate(positional):
        if a.arg in ("self", "cls", "request", "req"):
            continue
        ann_base = _base_annotation_name(a.annotation)
        # If annotated with a known Pydantic model -> expand its fields.
        if ann_base in models:
            params.extend(models[ann_base])
            continue
        default_node = None
        required = True
        if i >= default_offset:
            default_node = defaults[i - default_offset]
            required = False
        params.append(_make_param(a.arg, a.annotation, required, default_node))

    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if a.arg in ("self", "cls", "request", "req"):
            continue
        ann_base = _base_annotation_name(a.annotation)
        if ann_base in models:
            params.extend(models[ann_base])
            continue
        params.append(_make_param(a.arg, a.annotation, d is None, d))

    return params


def _func_returns(func: ast.FunctionDef | None) -> str:
    if func is None:
        return "unknown"
    if func.returns is not None:
        shape = _annotation_to_return_shape(func.returns)
        if shape != "unknown":
            return shape
    return _returns_shape_from_body(func)


def _make_capability(name, summary, params, returns, location, framework, http,
                     invocation, source_symbol) -> dict:
    risk = _classify_risk(name, http["method"] if http else None)
    return {
        "id": _slugify_name(name),
        "name": _slugify_name(name),
        "summary": summary,
        "params": params,
        "returns": returns,
        "location": location,
        "framework": framework,
        "http": http,
        "invocation": invocation,
        "risk": risk,
        "recommended": risk == "read",
        "source_symbol": source_symbol,
    }


def _humanize(name: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", name).replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else name


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
                names.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
                names.add(node.module)
            for a in node.names:
                names.add(a.name)
    return names


def _module_all(tree: ast.Module) -> list[str] | None:
    """Return the list assigned to `__all__` at module level, else None."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__":
                    val = node.value
                    if isinstance(val, (ast.List, ast.Tuple)):
                        out = []
                        for e in val.elts:
                            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                                out.append(e.value)
                        return out
    return None


# ---------------------------------------------------------------------------
# Django urls.py wiring
# ---------------------------------------------------------------------------
_URL_FUNCS = {"path", "re_path", "url"}


def _scan_django_urls(abs_path: str, rel_path: str) -> list[dict]:
    """Parse a urls.py and return [{'pattern','view','name'} ...] route specs.

    Handles `path('x', views.foo)`, `re_path(r'^x$', views.foo, name=...)`.
    Only same-module `views.<func>` references are useful for view-func wiring.
    """
    src = _read_source(abs_path)
    if src is None:
        return []
    try:
        tree = _safe_parse(src, abs_path)
    except (SyntaxError, ValueError):
        return []

    routes: list[dict] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in _URL_FUNCS:
            continue
        pattern = _first_str_arg(node)
        view = None
        # second positional arg is the view
        view_args = [a for a in node.args]
        if len(view_args) >= 2:
            v = view_args[1]
            if isinstance(v, ast.Attribute):      # views.foo
                view = v.attr
            elif isinstance(v, ast.Name):          # foo (imported directly)
                view = v.id
        name = None
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                name = kw.value.value
        if view:
            routes.append({"pattern": pattern, "view": view, "name": name})
    return routes


def _clean_django_path(pattern: str) -> str:
    """Turn a regex/route pattern into a usable HTTP path."""
    p = pattern.strip()
    p = p.lstrip("^").rstrip("$")
    # Django path() converters: <int:scan_id> already path-like; regex groups:
    p = re.sub(r"\(\?P<([^>]+)>[^)]*\)", r"{\1}", p)  # (?P<scan_id>...) -> {scan_id}
    p = re.sub(r"<[^:>]+:([^>]+)>", r"{\1}", p)         # <int:scan_id> -> {scan_id}
    p = re.sub(r"<([^>]+)>", r"{\1}", p)                 # <scan_id> -> {scan_id}
    if not p.startswith("/"):
        p = "/" + p
    return p


def _django_view_params(func: ast.FunctionDef | None) -> list[dict]:
    """Django views take `request` + URL kwargs. Surface the URL kwargs (after
    request) as params; the JSON body is opaque so we can't reliably type it."""
    if func is None:
        return []
    params: list[dict] = []
    args = func.args.args
    defaults = list(func.args.defaults)
    default_offset = len(args) - len(defaults)
    for i, a in enumerate(args):
        if a.arg in ("self", "cls", "request", "req"):
            continue
        default_node = None
        required = True
        if i >= default_offset:
            default_node = defaults[i - default_offset]
            required = False
        params.append(_make_param(a.arg, a.annotation, required, default_node))
    return params


def _scan_django_app(dir_abs: str, dir_rel: str, root: str,
                     capabilities: list, excluded: list) -> bool:
    """If a directory contains urls.py + views.py, wire views as Django HTTP caps.

    Returns True if Django routing was detected & emitted (so the plain-python
    scan can skip those view files)."""
    urls_path = os.path.join(dir_abs, "urls.py")
    if not os.path.isfile(urls_path):
        return False

    routes = _scan_django_urls(urls_path, os.path.relpath(urls_path, root))
    if not routes:
        return False

    # Parse all sibling .py files to find the view functions by name.
    view_funcs: dict[str, tuple[ast.FunctionDef, str]] = {}
    for fn in os.listdir(dir_abs):
        if not fn.endswith(".py") or fn == "urls.py":
            continue
        fabs = os.path.join(dir_abs, fn)
        frel = os.path.relpath(fabs, root)
        if _is_excluded_path(frel):
            continue
        src = _read_source(fabs)
        if src is None:
            continue
        try:
            tree = _safe_parse(src, fabs)
        except (SyntaxError, ValueError):
            excluded.append({"name": frel, "reason": "unparseable (syntax error)"})
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                view_funcs.setdefault(node.name, (node, frel))

    emitted = False
    seen_views: set[str] = set()
    for r in routes:
        view = r["view"]
        if view in seen_views:
            continue
        seen_views.add(view)
        vf = view_funcs.get(view)
        if not vf:
            # routed to a view we couldn't resolve in this dir (imported elsewhere)
            continue
        func, frel = vf
        http_path = _clean_django_path(r["pattern"])
        # Django function views handle any method internally; best-effort method
        # inference comes from the name via risk heuristics. Default method GET
        # for read-ish, POST otherwise.
        summary = _get_docstring_summary(func) or _humanize(view)
        params = _django_view_params(func)
        risk = _classify_risk(view, None)
        method = "GET" if risk == "read" else ("DELETE" if risk == "destructive" else "POST")
        capabilities.append(_make_capability(
            name=view,
            summary=summary,
            params=params,
            returns=_func_returns(func),
            location=f"{frel}:{func.lineno}",
            framework="django",
            http={"method": method, "path": http_path},
            invocation="http",
            source_symbol=view,
        ))
        emitted = True

    return emitted


# ---------------------------------------------------------------------------
# Project / language detection
# ---------------------------------------------------------------------------
def _detect_language_and_frameworks(root: str) -> tuple[str, set]:
    """Read manifest files + scan extensions to guess language + frameworks."""
    frameworks: set[str] = set()
    language = "unknown"

    has_py = False
    has_ts = False
    has_js = False

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                has_py = True
            elif fn.endswith((".ts", ".tsx")):
                has_ts = True
            elif fn.endswith((".js", ".jsx", ".mjs")):
                has_js = True

    # Manifests
    req_txt = os.path.join(root, "requirements.txt")
    pyproject = os.path.join(root, "pyproject.toml")
    setup_py = os.path.join(root, "setup.py")
    pkg_json = os.path.join(root, "package.json")
    manage_py = os.path.join(root, "manage.py")

    manifest_text = ""
    for m in (req_txt, pyproject, setup_py):
        if os.path.isfile(m):
            txt = _read_source(m) or ""
            manifest_text += "\n" + txt.lower()

    if "fastapi" in manifest_text:
        frameworks.add("fastapi")
    if re.search(r"(^|\n|\s)flask", manifest_text):
        frameworks.add("flask")
    if "django" in manifest_text or os.path.isfile(manage_py):
        frameworks.add("django")

    if has_py:
        language = "python"
    elif has_ts:
        language = "typescript"
    elif has_js:
        language = "javascript"

    if os.path.isfile(pkg_json):
        if language == "unknown":
            language = "javascript"

    return language, frameworks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def discover(repo_path: str) -> dict:
    """Scan a repo and return its capabilities.

    See module docstring / CONTRACT.md for the return shape.
    """
    root = os.path.abspath(repo_path)
    capabilities: list[dict] = []
    excluded: list[dict] = []

    if not os.path.isdir(root):
        return {
            "project": {"language": "unknown", "frameworks": [], "root": root},
            "capabilities": [],
            "excluded": [{"name": root, "reason": "path is not a directory"}],
        }

    language, frameworks = _detect_language_and_frameworks(root)

    # Pass 1: Django app wiring (dirs with urls.py + views.py). Track which
    # view files we've covered so the plain-python pass skips request-takers
    # gracefully (they self-exclude anyway).
    django_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        if "urls.py" in filenames:
            try:
                if _scan_django_app(dirpath, os.path.relpath(dirpath, root), root,
                                    capabilities, excluded):
                    frameworks.add("django")
                    django_dirs.add(os.path.abspath(dirpath))
            except Exception as exc:  # never crash the scan on one app
                excluded.append({
                    "name": os.path.relpath(dirpath, root),
                    "reason": f"django url scan failed: {exc.__class__.__name__}",
                })

    # Names already emitted as Django/route caps — avoid dupes from pass 2.
    emitted_symbols = {c["source_symbol"] for c in capabilities}

    # Pass 2: per-file scan for FastAPI/Flask routes + plain-python functions.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            abs_path = os.path.join(dirpath, fn)
            rel_path = os.path.relpath(abs_path, root)
            # urls.py handled in pass 1
            if fn == "urls.py" and os.path.abspath(dirpath) in django_dirs:
                continue
            try:
                before = len(capabilities)
                _scan_python_file(abs_path, rel_path, capabilities, excluded, frameworks,
                                  routed_symbols=emitted_symbols)
                # de-dupe any caps already emitted via Django wiring
                if os.path.abspath(dirpath) in django_dirs:
                    new = capabilities[before:]
                    del capabilities[before:]
                    for c in new:
                        if c["source_symbol"] in emitted_symbols:
                            continue
                        capabilities.append(c)
                        emitted_symbols.add(c["source_symbol"])
                else:
                    for c in capabilities[before:]:
                        emitted_symbols.add(c["source_symbol"])
            except Exception as exc:  # robustness: never crash on one file
                excluded.append({"name": rel_path, "reason": f"scan error: {exc.__class__.__name__}"})

    # De-duplicate capabilities by (id, location); keep first occurrence.
    seen: set = set()
    deduped: list[dict] = []
    for c in capabilities:
        key = (c["id"], c["location"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    return {
        "project": {
            "language": language,
            "frameworks": sorted(frameworks),
            "root": root,
        },
        "capabilities": deduped,
        "excluded": excluded,
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(discover(target), indent=2))
