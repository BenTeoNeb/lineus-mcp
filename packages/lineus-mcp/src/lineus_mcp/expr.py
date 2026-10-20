"""Sandboxed expressions behind parametric curves: a whitelisted AST walk, no exec."""
from __future__ import annotations

import ast
import math

# The scene language is built on an expression evaluator, NOT a catalogue of shapes.
# A fixed vocabulary of circle/ellipse/arc would handle the dull cases and send every
# interesting drawing straight back to pasting raw coordinates, which is the problem
# this is meant to solve. So there is no circle primitive: a circle is
#     {"param": {"t": [0, 6.2832, 200], "x": "33+5.5*cos(t)", "y": "30+5.5*sin(t)"}}
# and so are the spiral, the rose, the lissajous and the harmonograph that actually
# ate the tokens. The vocabulary is open because it is a function evaluator.
#
# Safety: a whitelisted AST walk, not eval() on arbitrary input. No imports, no
# attribute access, no indexing, no comprehensions, no calls except the names below.
# That keeps the public server free of an exec() interface while leaving the
# expressiveness where it matters.
EXPR_FUNCS = {
    "abs": abs, "min": min, "max": max, "round": round, "pow": pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "exp": math.exp, "log": math.log, "sqrt": math.sqrt, "hypot": math.hypot,
    "floor": math.floor, "ceil": math.ceil, "fmod": math.fmod,
    "degrees": math.degrees, "radians": math.radians,
    "sign": lambda x: (x > 0) - (x < 0),
    "clamp": lambda x, lo, hi: lo if x < lo else (hi if x > hi else x),
    "lerp": lambda a, b, s: a + (b - a) * s,
}


EXPR_CONSTS = {"pi": math.pi, "tau": math.tau, "e": math.e}


_EXPR_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.IfExp,
    ast.Call, ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.And, ast.Or,
)


class ExprError(ValueError):
    pass


class Expr:
    """A compiled, sandboxed scalar expression over named variables."""

    __slots__ = ("src", "_code", "_names")

    def __init__(self, src: str, variables: tuple[str, ...]):
        self.src = src
        try:
            tree = ast.parse(src, mode="eval")
        except SyntaxError as e:
            raise ExprError(f"{src!r}: {e.msg}") from None
        allowed = set(variables) | set(EXPR_FUNCS) | set(EXPR_CONSTS)
        for node in ast.walk(tree):
            if not isinstance(node, _EXPR_NODES):
                raise ExprError(
                    f"{src!r}: {type(node).__name__} is not allowed in an expression")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in EXPR_FUNCS:
                    raise ExprError(f"{src!r}: only these calls are allowed: "
                                    f"{', '.join(sorted(EXPR_FUNCS))}")
                if node.keywords:
                    raise ExprError(f"{src!r}: keyword arguments are not allowed")
            if isinstance(node, ast.Name) and node.id not in allowed:
                raise ExprError(f"{src!r}: unknown name {node.id!r}. Available here: "
                                f"{', '.join(sorted(allowed))}")
        self._names = tuple(variables)
        self._code = compile(tree, "<expr>", "eval")

    def __call__(self, **vals) -> float:
        env = dict(EXPR_CONSTS)
        env.update(EXPR_FUNCS)
        env.update(vals)
        try:
            return float(eval(self._code, {"__builtins__": {}}, env))  # noqa: S307
        except ExprError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ExprError(f"{self.src!r} failed at {vals}: {type(e).__name__}: {e}") from None
