"""Route checks of an app: reserved core roots and code owned by its package (Django inside functions)."""

import re

RESERVED_ROOTS = ("admin", "platform", "notifications")


def _validate_reserved_routes(patterns, prefix=""):
    """Apps cannot capture core-owned roots: platform/ (administration) and the retired admin/."""
    from django.core.exceptions import ValidationError
    from django.urls import URLResolver
    from django.urls.resolvers import RoutePattern

    for pattern in patterns:
        if isinstance(pattern.pattern, RoutePattern):
            route = prefix + pattern.pattern._route
            first = route.split("/", 1)[0]
            if first in RESERVED_ROOTS:
                raise ValidationError(f"Application URLs collide with reserved {first}/ prefix.")
            if "<" in first:
                # Test just the first segment, independently of later path parameters.
                from django.urls.resolvers import RoutePattern as SegmentPattern

                for root in RESERVED_ROOTS:
                    if SegmentPattern(first + "/").regex.match(root + "/") is not None:
                        raise ValidationError(f"Application URLs capture the reserved {root}/ prefix.")
            if not first and isinstance(pattern, URLResolver):
                _validate_reserved_routes(pattern.url_patterns, route)
        else:
            expression = pattern.pattern.regex.pattern
            if expression in ("^$", "$", "") and not isinstance(pattern, URLResolver):
                continue
            if expression in ("^", "") and isinstance(pattern, URLResolver):
                _validate_reserved_routes(pattern.url_patterns, prefix)
                continue
            # Regex routes must prove a disjoint leading literal; catchalls/branches
            # cannot safely share the root with a core-owned administration prefix.
            literal = re.match(r"\^?([a-zA-Z0-9_/-]*)", expression).group(1)
            suffix = expression[len(literal) + (1 if expression.startswith("^") else 0) :]
            if suffix.startswith(("?", "*", "{")):
                literal = literal[:-1]
            route = (prefix + literal).casefold()
            if (
                not expression.startswith("^")
                or not route
                or any(f"{root}/".startswith(route) or route.startswith(f"{root}/") for root in RESERVED_ROOTS)
                or "|" in expression
            ):
                raise ValidationError("Application regex URLs may capture a reserved prefix (admin/, platform/).")


def _validate_owned_routes(patterns, package):
    from django.core.exceptions import ValidationError
    from django.urls import URLResolver

    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            module = pattern.urlconf_name
            name = module if isinstance(module, str) else getattr(module, "__name__", None)
            if name and not (name == package or name.startswith(package + ".")):
                raise ValidationError("Application URL includes must belong to its package.")
            _validate_owned_routes(pattern.url_patterns, package)
        else:
            module = getattr(pattern.callback, "__module__", "")
            if not (module == package or module.startswith(package + ".")):
                raise ValidationError("Application URL callbacks must belong to its package.")
