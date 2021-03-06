from functools import wraps
import sys

from typing import Tuple, Union, TypeVar, Callable, Sequence
from mypy.nodes import (
    MypyFile, Import, ImportAll, ImportFrom, FuncDef, OverloadedFuncDef,
    ClassDef, Decorator, Block, Var, OperatorAssignmentStmt,
    ExpressionStmt, AssignmentStmt, ReturnStmt, RaiseStmt, AssertStmt,
    DelStmt, BreakStmt, ContinueStmt, PassStmt, GlobalDecl,
    WhileStmt, ForStmt, IfStmt, TryStmt, WithStmt,
    TupleExpr, GeneratorExpr, ListComprehension, ListExpr, ConditionalExpr,
    DictExpr, SetExpr, NameExpr, IntExpr, StrExpr, BytesExpr,
    FloatExpr, CallExpr, SuperExpr, MemberExpr, IndexExpr, SliceExpr, OpExpr,
    UnaryExpr, FuncExpr, ComparisonExpr,
    StarExpr, YieldFromExpr, NonlocalDecl, DictionaryComprehension,
    SetComprehension, ComplexExpr, EllipsisExpr, YieldExpr, Argument,
    ARG_POS, ARG_OPT, ARG_STAR, ARG_NAMED, ARG_STAR2
)
from mypy.types import Type, CallableType, AnyType, UnboundType, TupleType, TypeList, EllipsisType
from mypy import defaults
from mypy.errors import Errors

try:
    import typed_ast  # type: ignore
except ImportError:
    print('You must install the typed_ast module before you can run mypy with `--fast-parser`.\n'
          'The typed_ast module can be found at https://github.com/ddfisher/typed_ast',
          file=sys.stderr)
    sys.exit(1)

T = TypeVar('T')


def parse(source: Union[str, bytes], fnam: str = None, errors: Errors = None,
          pyversion: Tuple[int, int] = defaults.PYTHON3_VERSION,
          custom_typing_module: str = None) -> MypyFile:
    """Parse a source file, without doing any semantic analysis.

    Return the parse tree. If errors is not provided, raise ParseError
    on failure. Otherwise, use the errors object to report parse errors.

    The pyversion (major, minor) argument determines the Python syntax variant.
    """
    is_stub_file = bool(fnam) and fnam.endswith('.pyi')
    try:
        ast = typed_ast.parse(source, fnam, 'exec')
    except SyntaxError as e:
        if errors:
            errors.set_file('<input>' if fnam is None else fnam)
            errors.report(e.lineno, e.msg)  # type: ignore
        else:
            raise
    else:
        tree = ASTConverter().visit(ast)
        tree.path = fnam
        tree.is_stub = is_stub_file
        return tree

    return MypyFile([],
                    [],
                    False,
                    set(),
                    weak_opts=set())


def parse_type_comment(type_comment: str, line: int) -> Type:
    typ = typed_ast.parse(type_comment, '<type_comment>', 'eval')
    return TypeConverter(line=line).visit(typ.body)


def with_line(f):
    @wraps(f)
    def wrapper(self, ast):
        node = f(self, ast)
        node.set_line(ast.lineno)
        return node
    return wrapper


def find(f: Callable[[T], bool], seq: Sequence[T]) -> T:
    for item in seq:
        if f(item):
            return item
    return None


class ASTConverter(typed_ast.NodeTransformer):
    def __init__(self):
        self.in_class = False
        self.imports = []

    def generic_visit(self, node):
        raise RuntimeError('AST node not implemented: ' + str(type(node)))

    def visit_NoneType(self, n):
        return None

    def visit_list(self, l):
        return [self.visit(e) for e in l]

    op_map = {
        typed_ast.Add: '+',
        typed_ast.Sub: '-',
        typed_ast.Mult: '*',
        typed_ast.MatMult: '@',
        typed_ast.Div: '/',
        typed_ast.Mod: '%',
        typed_ast.Pow: '**',
        typed_ast.LShift: '<<',
        typed_ast.RShift: '>>',
        typed_ast.BitOr: '|',
        typed_ast.BitXor: '^',
        typed_ast.BitAnd: '&',
        typed_ast.FloorDiv: '//'
    }

    def from_operator(self, op):
        op_name = ASTConverter.op_map.get(type(op))
        if op_name is None:
            raise RuntimeError('Unknown operator ' + str(type(op)))
        elif op_name == '@':
            raise RuntimeError('mypy does not support the MatMult operator')
        else:
            return op_name

    comp_op_map = {
        typed_ast.Gt: '>',
        typed_ast.Lt: '<',
        typed_ast.Eq: '==',
        typed_ast.GtE: '>=',
        typed_ast.LtE: '<=',
        typed_ast.NotEq: '!=',
        typed_ast.Is: 'is',
        typed_ast.IsNot: 'is not',
        typed_ast.In: 'in',
        typed_ast.NotIn: 'not in'
    }

    def from_comp_operator(self, op):
        op_name = ASTConverter.comp_op_map.get(type(op))
        if op_name is None:
            raise RuntimeError('Unknown comparison operator ' + str(type(op)))
        else:
            return op_name

    def as_block(self, stmts, lineno):
        b = None
        if stmts:
            b = Block(self.visit(stmts))
            b.set_line(lineno)
        return b

    def fix_function_overloads(self, stmts):
        ret = []
        current_overload = []
        current_overload_name = None
        # mypy doesn't actually check that the decorator is literally @overload
        for stmt in stmts:
            if isinstance(stmt, Decorator) and stmt.name() == current_overload_name:
                current_overload.append(stmt)
            else:
                if len(current_overload) == 1:
                    ret.append(current_overload[0])
                elif len(current_overload) > 1:
                    ret.append(OverloadedFuncDef(current_overload))

                if isinstance(stmt, Decorator):
                    current_overload = [stmt]
                    current_overload_name = stmt.name()
                else:
                    current_overload = []
                    current_overload_name = None
                    ret.append(stmt)

        if len(current_overload) == 1:
            ret.append(current_overload[0])
        elif len(current_overload) > 1:
            ret.append(OverloadedFuncDef(current_overload))
        return ret

    def visit_Module(self, mod):
        body = self.fix_function_overloads(self.visit(mod.body))

        return MypyFile(body,
                        self.imports,
                        False,
                        {ti.lineno for ti in mod.type_ignores},
                        weak_opts=set())

    # --- stmt ---
    # FunctionDef(identifier name, arguments args,
    #             stmt* body, expr* decorator_list, expr? returns, string? type_comment)
    # arguments = (arg* args, arg? vararg, arg* kwonlyargs, expr* kw_defaults,
    #              arg? kwarg, expr* defaults)
    @with_line
    def visit_FunctionDef(self, n):
        args = self.transform_args(n.args, n.lineno)

        arg_kinds = [arg.kind for arg in args]
        arg_names = [arg.variable.name() for arg in args]
        if n.type_comment is not None:
            func_type_ast = typed_ast.parse(n.type_comment, '<func_type>', 'func_type')
            arg_types = [a if a is not None else AnyType() for
                         a in TypeConverter(line=n.lineno).visit(func_type_ast.argtypes)]
            return_type = TypeConverter(line=n.lineno).visit(func_type_ast.returns)

            # add implicit self type
            if self.in_class and len(arg_types) < len(args):
                arg_types.insert(0, AnyType())
        else:
            arg_types = [a.type_annotation for a in args]
            return_type = TypeConverter(line=n.lineno).visit(n.returns)

        func_type = None
        if any(arg_types) or return_type:
            func_type = CallableType([a if a is not None else AnyType() for a in arg_types],
                                     arg_kinds,
                                     arg_names,
                                     return_type if return_type is not None else AnyType(),
                                     None)

        func_def = FuncDef(n.name,
                       args,
                       self.as_block(n.body, n.lineno),
                       func_type)
        if func_type is not None:
            func_type.definition = func_def

        if n.decorator_list:
            var = Var(func_def.name())
            var.is_ready = False
            var.set_line(n.decorator_list[0].lineno)

            func_def.is_decorated = True
            func_def.set_line(n.lineno + len(n.decorator_list))
            func_def.body.set_line(func_def.get_line())
            return Decorator(func_def, self.visit(n.decorator_list), var)
        else:
            return func_def

    def transform_args(self, args, line):
        def make_argument(arg, default, kind):
            arg_type = TypeConverter(line=line).visit(arg.annotation)
            return Argument(Var(arg.arg), arg_type, self.visit(default), kind)

        new_args = []
        num_no_defaults = len(args.args) - len(args.defaults)
        # positional arguments without defaults
        for a in args.args[:num_no_defaults]:
            new_args.append(make_argument(a, None, ARG_POS))

        # positional arguments with defaults
        for a, d in zip(args.args[num_no_defaults:], args.defaults):
            new_args.append(make_argument(a, d, ARG_OPT))

        # *arg
        if args.vararg is not None:
            new_args.append(make_argument(args.vararg, None, ARG_STAR))

        num_no_kw_defaults = len(args.kwonlyargs) - len(args.kw_defaults)
        # keyword-only arguments without defaults
        for a in args.kwonlyargs[:num_no_kw_defaults]:
            new_args.append(make_argument(a, None, ARG_NAMED))

        # keyword-only arguments with defaults
        for a, d in zip(args.kwonlyargs[num_no_kw_defaults:], args.kw_defaults):
            new_args.append(make_argument(a, d, ARG_NAMED))

        # **kwarg
        if args.kwarg is not None:
            new_args.append(make_argument(args.kwarg, None, ARG_STAR2))

        return new_args

    # TODO: AsyncFunctionDef(identifier name, arguments args,
    #                  stmt* body, expr* decorator_list, expr? returns, string? type_comment)

    def stringify_name(self, n):
        if isinstance(n, typed_ast.Name):
            return n.id
        elif isinstance(n, typed_ast.Attribute):
            return "{}.{}".format(self.stringify_name(n.value), n.attr)
        else:
            assert False, "can't stringify " + str(type(n))

    # ClassDef(identifier name,
    #  expr* bases,
    #  keyword* keywords,
    #  stmt* body,
    #  expr* decorator_list)
    @with_line
    def visit_ClassDef(self, n):
        self.in_class = True
        metaclass_arg = find(lambda x: x.arg == 'metaclass', n.keywords)
        metaclass = None
        if metaclass_arg:
            metaclass = self.stringify_name(metaclass_arg.value)

        cdef = ClassDef(n.name,
                        Block(self.fix_function_overloads(self.visit(n.body))),
                        None,
                        self.visit(n.bases),
                        metaclass=metaclass)
        cdef.decorators = self.visit(n.decorator_list)
        self.in_class = False
        return cdef

    # Return(expr? value)
    @with_line
    def visit_Return(self, n):
        return ReturnStmt(self.visit(n.value))

    # Delete(expr* targets)
    @with_line
    def visit_Delete(self, n):
        if len(n.targets) > 1:
            tup = TupleExpr(self.visit(n.targets))
            tup.set_line(n.lineno)
            return DelStmt(tup)
        else:
            return DelStmt(self.visit(n.targets[0]))

    # Assign(expr* targets, expr value, string? type_comment)
    @with_line
    def visit_Assign(self, n):
        typ = None
        if n.type_comment:
            typ = parse_type_comment(n.type_comment, n.lineno)

        return AssignmentStmt(self.visit(n.targets),
                              self.visit(n.value),
                              type=typ)

    # AugAssign(expr target, operator op, expr value)
    @with_line
    def visit_AugAssign(self, n):
        return OperatorAssignmentStmt(self.from_operator(n.op),
                              self.visit(n.target),
                              self.visit(n.value))

    # For(expr target, expr iter, stmt* body, stmt* orelse, string? type_comment)
    @with_line
    def visit_For(self, n):
        return ForStmt(self.visit(n.target),
                       self.visit(n.iter),
                       self.as_block(n.body, n.lineno),
                       self.as_block(n.orelse, n.lineno))

    # TODO: AsyncFor(expr target, expr iter, stmt* body, stmt* orelse)
    # While(expr test, stmt* body, stmt* orelse)
    @with_line
    def visit_While(self, n):
        return WhileStmt(self.visit(n.test),
                         self.as_block(n.body, n.lineno),
                         self.as_block(n.orelse, n.lineno))

    # If(expr test, stmt* body, stmt* orelse)
    @with_line
    def visit_If(self, n):
        return IfStmt([self.visit(n.test)],
                      [self.as_block(n.body, n.lineno)],
                      self.as_block(n.orelse, n.lineno))

    # With(withitem* items, stmt* body, string? type_comment)
    @with_line
    def visit_With(self, n):
        return WithStmt([self.visit(i.context_expr) for i in n.items],
                        [self.visit(i.optional_vars) for i in n.items],
                        self.as_block(n.body, n.lineno))

    # TODO: AsyncWith(withitem* items, stmt* body)

    # Raise(expr? exc, expr? cause)
    @with_line
    def visit_Raise(self, n):
        return RaiseStmt(self.visit(n.exc), self.visit(n.cause))

    # Try(stmt* body, excepthandler* handlers, stmt* orelse, stmt* finalbody)
    @with_line
    def visit_Try(self, n):
        vs = [NameExpr(h.name) if h.name is not None else None for h in n.handlers]
        types = [self.visit(h.type) for h in n.handlers]
        handlers = [self.as_block(h.body, h.lineno) for h in n.handlers]

        return TryStmt(self.as_block(n.body, n.lineno),
                       vs,
                       types,
                       handlers,
                       self.as_block(n.orelse, n.lineno),
                       self.as_block(n.finalbody, n.lineno))

    # Assert(expr test, expr? msg)
    @with_line
    def visit_Assert(self, n):
        return AssertStmt(self.visit(n.test))

    # Import(alias* names)
    @with_line
    def visit_Import(self, n):
        i = Import([(a.name, a.asname) for a in n.names])
        self.imports.append(i)
        return i

    # ImportFrom(identifier? module, alias* names, int? level)
    @with_line
    def visit_ImportFrom(self, n):
        if len(n.names) == 1 and n.names[0].name == '*':
            i = ImportAll(n.module, n.level)
        else:
            i = ImportFrom(n.module if n.module is not None else '',
                           n.level,
                           [(a.name, a.asname) for a in n.names])
        self.imports.append(i)
        return i

    # Global(identifier* names)
    @with_line
    def visit_Global(self, n):
        return GlobalDecl(n.names)

    # Nonlocal(identifier* names)
    @with_line
    def visit_Nonlocal(self, n):
        return NonlocalDecl(n.names)

    # Expr(expr value)
    @with_line
    def visit_Expr(self, expr):
        value = self.visit(expr.value)
        return ExpressionStmt(value)

    # Pass
    @with_line
    def visit_Pass(self, n):
        return PassStmt()

    # Break
    @with_line
    def visit_Break(self, n):
        return BreakStmt()

    # Continue
    @with_line
    def visit_Continue(self, n):
        return ContinueStmt()

    # --- expr ---
    # BoolOp(boolop op, expr* values)
    @with_line
    def visit_BoolOp(self, n):
        # mypy translates (1 and 2 and 3) as (1 and (2 and 3))
        assert len(n.values) >= 2
        op = None
        if isinstance(n.op, typed_ast.And):
            op = 'and'
        elif isinstance(n.op, typed_ast.Or):
            op = 'or'
        else:
            raise RuntimeError('unknown BoolOp ' + str(type(n)))

        # potentially inefficient!
        def group(vals):
            if len(vals) == 2:
                return OpExpr(op, vals[0], vals[1])
            else:
                return OpExpr(op, vals[0], group(vals[1:]))

        return group(self.visit(n.values))

    # BinOp(expr left, operator op, expr right)
    @with_line
    def visit_BinOp(self, n):
        op = self.from_operator(n.op)

        if op is None:
            raise RuntimeError('cannot translate BinOp ' + str(type(n.op)))

        return OpExpr(op, self.visit(n.left), self.visit(n.right))

    # UnaryOp(unaryop op, expr operand)
    @with_line
    def visit_UnaryOp(self, n):
        op = None
        if isinstance(n.op, typed_ast.Invert):
            op = '~'
        elif isinstance(n.op, typed_ast.Not):
            op = 'not'
        elif isinstance(n.op, typed_ast.UAdd):
            op = '+'
        elif isinstance(n.op, typed_ast.USub):
            op = '-'

        if op is None:
            raise RuntimeError('cannot translate UnaryOp ' + str(type(n.op)))

        return UnaryExpr(op, self.visit(n.operand))

    # Lambda(arguments args, expr body)
    @with_line
    def visit_Lambda(self, n):
        body = typed_ast.Return(n.body)
        body.lineno = n.lineno

        return FuncExpr(self.transform_args(n.args, n.lineno),
                        self.as_block([body], n.lineno))

    # IfExp(expr test, expr body, expr orelse)
    @with_line
    def visit_IfExp(self, n):
        return ConditionalExpr(self.visit(n.test),
                               self.visit(n.body),
                               self.visit(n.orelse))

    # Dict(expr* keys, expr* values)
    @with_line
    def visit_Dict(self, n):
        return DictExpr(list(zip(self.visit(n.keys), self.visit(n.values))))

    # Set(expr* elts)
    @with_line
    def visit_Set(self, n):
        return SetExpr(self.visit(n.elts))

    # ListComp(expr elt, comprehension* generators)
    @with_line
    def visit_ListComp(self, n):
        return ListComprehension(self.visit_GeneratorExp(n))

    # SetComp(expr elt, comprehension* generators)
    @with_line
    def visit_SetComp(self, n):
        return SetComprehension(self.visit_GeneratorExp(n))

    # DictComp(expr key, expr value, comprehension* generators)
    @with_line
    def visit_DictComp(self, n):
        targets = [self.visit(c.target) for c in n.generators]
        iters = [self.visit(c.iter) for c in n.generators]
        ifs_list = [self.visit(c.ifs) for c in n.generators]
        return DictionaryComprehension(self.visit(n.key),
                                       self.visit(n.value),
                                       targets,
                                       iters,
                                       ifs_list)

    # GeneratorExp(expr elt, comprehension* generators)
    @with_line
    def visit_GeneratorExp(self, n):
        targets = [self.visit(c.target) for c in n.generators]
        iters = [self.visit(c.iter) for c in n.generators]
        ifs_list = [self.visit(c.ifs) for c in n.generators]
        return GeneratorExpr(self.visit(n.elt),
                             targets,
                             iters,
                             ifs_list)

    # TODO: Await(expr value)

    # Yield(expr? value)
    @with_line
    def visit_Yield(self, n):
        return YieldExpr(self.visit(n.value))

    # YieldFrom(expr value)
    @with_line
    def visit_YieldFrom(self, n):
        return YieldFromExpr(self.visit(n.value))

    # Compare(expr left, cmpop* ops, expr* comparators)
    @with_line
    def visit_Compare(self, n):
        operators = [self.from_comp_operator(o) for o in n.ops]
        operands = self.visit([n.left] + n.comparators)
        return ComparisonExpr(operators, operands)

    # Call(expr func, expr* args, keyword* keywords)
    # keyword = (identifier? arg, expr value)
    @with_line
    def visit_Call(self, n):
        def is_stararg(a):
            return isinstance(a, typed_ast.Starred)

        def is_star2arg(k):
            return k.arg is None

        arg_types = self.visit([a.value if is_stararg(a) else a for a in n.args] +
                               [k.value for k in n.keywords])
        arg_kinds = ([ARG_STAR if is_stararg(a) else ARG_POS for a in n.args] +
                     [ARG_STAR2 if is_star2arg(k) else ARG_NAMED for k in n.keywords])
        return CallExpr(self.visit(n.func),
                        arg_types,
                        arg_kinds,
                        [None for _ in n.args] + [k.arg for k in n.keywords])

    # Num(object n) -- a number as a PyObject.
    @with_line
    def visit_Num(self, num):
        if isinstance(num.n, int):
            return IntExpr(num.n)
        elif isinstance(num.n, float):
            return FloatExpr(num.n)
        elif isinstance(num.n, complex):
            return ComplexExpr(num.n)

        raise RuntimeError('num not implemented for ' + str(type(num.n)))

    # Str(string s) -- need to specify raw, unicode, etc?
    @with_line
    def visit_Str(self, n):
        return StrExpr(n.s)

    # Bytes(bytes s)
    @with_line
    def visit_Bytes(self, n):
        # TODO: this is kind of hacky
        return BytesExpr(str(n.s)[2:-1])

    # NameConstant(singleton value)
    def visit_NameConstant(self, n):
        return NameExpr(str(n.value))

    # Ellipsis
    @with_line
    def visit_Ellipsis(self, n):
        return EllipsisExpr()

    # Attribute(expr value, identifier attr, expr_context ctx)
    @with_line
    def visit_Attribute(self, n):
        if (isinstance(n.value, typed_ast.Call) and
                isinstance(n.value.func, typed_ast.Name) and
                n.value.func.id == 'super'):
            return SuperExpr(n.attr)

        return MemberExpr(self.visit(n.value), n.attr)

    # Subscript(expr value, slice slice, expr_context ctx)
    @with_line
    def visit_Subscript(self, n):
        return IndexExpr(self.visit(n.value), self.visit(n.slice))

    # Starred(expr value, expr_context ctx)
    @with_line
    def visit_Starred(self, n):
        return StarExpr(self.visit(n.value))

    # Name(identifier id, expr_context ctx)
    @with_line
    def visit_Name(self, n):
        return NameExpr(n.id)

    # List(expr* elts, expr_context ctx)
    @with_line
    def visit_List(self, n):
        return ListExpr([self.visit(e) for e in n.elts])

    # Tuple(expr* elts, expr_context ctx)
    @with_line
    def visit_Tuple(self, n):
        return TupleExpr([self.visit(e) for e in n.elts])

    # --- slice ---

    # Slice(expr? lower, expr? upper, expr? step)
    def visit_Slice(self, n):
        return SliceExpr(self.visit(n.lower),
                         self.visit(n.upper),
                         self.visit(n.step))

    # ExtSlice(slice* dims)
    def visit_ExtSlice(self, n):
        return TupleExpr(self.visit(n.dims))

    # Index(expr value)
    def visit_Index(self, n):
        return self.visit(n.value)


class TypeConverter(typed_ast.NodeTransformer):
    def __init__(self, line=-1):
        self.line = line

    def generic_visit(self, node):
        raise RuntimeError('Type node not implemented: ' + str(type(node)))

    def visit_NoneType(self, n):
        return None

    def visit_list(self, l):
        return [self.visit(e) for e in l]

    def visit_Name(self, n):
        return UnboundType(n.id, line=self.line)

    def visit_NameConstant(self, n):
        return UnboundType(str(n.value))

    # Str(string s)
    def visit_Str(self, n):
        return parse_type_comment(n.s.strip(), line=self.line)

    # Subscript(expr value, slice slice, expr_context ctx)
    def visit_Subscript(self, n):
        assert isinstance(n.slice, typed_ast.Index)

        value = self.visit(n.value)

        assert isinstance(value, UnboundType)
        assert not value.args

        if isinstance(n.slice.value, typed_ast.Tuple):
            params = self.visit(n.slice.value.elts)
        else:
            params = [self.visit(n.slice.value)]

        return UnboundType(value.name, params, line=self.line)

    def visit_Tuple(self, n):
        return TupleType(self.visit(n.elts), None, implicit=True, line=self.line)

    # Attribute(expr value, identifier attr, expr_context ctx)
    def visit_Attribute(self, n):
        before_dot = self.visit(n.value)

        assert isinstance(before_dot, UnboundType)
        assert not before_dot.args

        return UnboundType("{}.{}".format(before_dot.name, n.attr), line=self.line)

    # Ellipsis
    def visit_Ellipsis(self, n):
        return EllipsisType(line=self.line)

    # List(expr* elts, expr_context ctx)
    def visit_List(self, n):
        return TypeList(self.visit(n.elts), line=self.line)
