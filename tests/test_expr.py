import math

import pytest

from lineus_mcp.expr import Expr, ExprError


def test_arithmetic_and_math():
    assert Expr("2*t+1", ("t",))(t=3) == 7
    assert Expr("sqrt(hypot(3,4))", ("t",))(t=0) == pytest.approx(math.sqrt(5))
    assert Expr("cos(pi)", ("t",))(t=0) == pytest.approx(-1)
    assert Expr("1 if t>0 else -1", ("t",))(t=5) == 1


@pytest.mark.parametrize("src", [
    "__import__('os').system('x')",      # import
    "t.__class__",                       # attribute access
    "[x for x in range(9)]",             # comprehension
    "open('/etc/passwd')",               # a call that is not whitelisted
    "(1).__class__",                     # attribute on a literal
    "lambda: 1",                         # lambda
])
def test_sandbox_rejects(src):
    with pytest.raises(ExprError):
        Expr(src, ("t",))


def test_unknown_name_lists_what_is_available():
    with pytest.raises(ExprError, match="unknown name 'zzz'.*sin"):
        Expr("zzz*2", ("t",))
