import base64
import hashlib
import marshal
import math
import os.path
import random
import sys
import zlib
from _ast import Module, Call
from types import CodeType
from typing import Callable, Any

import rich
from Crypto.Cipher import AES

from cfg import ConfigSegment, ConfigValue
from ast import *

from renamer import MappingGenerator, MappingApplicator, OtherFileMappingApplicator
from util import ast_import_full
from util import randomize_cache, ast_import_from


class Transformer(object):
    def __init__(self, name: str, desc: str, **add_config: ConfigValue):
        self.name = name
        self.config = ConfigSegment(self.name, desc,
                                    enabled=ConfigValue("Enables this transformer", False),
                                    **add_config)
        self.console: rich.Console = None

    def transform(self, ast: AST, current_file_name, all_asts, all_file_names) -> AST:
        return ast


def compute_import_path(from_path: str, to_path: str):
    common_prefix = len(os.path.commonpath(
        [
            os.path.dirname(from_path),
            os.path.dirname(to_path)
        ]
    ))
    from_path = from_path[common_prefix+1:].split(os.path.sep)
    to_path = to_path[common_prefix+1:].split(os.path.sep)

    full_imp = ""
    while len(from_path) > 1:
        full_imp += ".."
        from_path.pop(0)
    if to_path[len(to_path)-1] == "__init__.py":
        to_path.pop()

    for x in to_path:
        full_imp += x + "."
    full_imp = full_imp[:-1]
    if full_imp.endswith(".py"):
        full_imp = full_imp[:-3]
    return full_imp


class MemberRenamer(Transformer):
    def __init__(self):
        super().__init__("renamer", "Renames all members (methods, classes, fields, args)",
                         rename_format=ConfigValue("Format for the renamer. Will be queried using eval().\n"
                                                   "'counter' is a variable incrementing with each name generated\n"
                                                   "'kind' is either 'method', 'var', 'arg' or 'class', depending on the current element\n"
                                                   "'get_counter(name)' is a method that increments a counter behind 'name', and returns its current "
                                                   "value",
                                                   "f'{kind}{get_counter(kind)}'"))

    def transform(self, ast: AST, current_file_name, all_asts, all_file_names) -> AST:
        generator = MappingGenerator(self.config["rename_format"].value)
        generator.visit(ast)
        MappingApplicator(generator.mappings).visit(ast)
        if all_asts is not None:
            mappings1 = {}
            this_file_name = os.path.abspath(current_file_name)
            for x in generator.mappings.keys():
                n = x.split(".")
                if n[0] == "":
                    # self.console.log(current_file_name, n)
                    mappings1[n[1]] = generator.mappings[x]
            for i in range(len(all_asts)):
                that_ast = all_asts[i]
                if that_ast == ast:
                    continue
                that_file_name = os.path.abspath(all_file_names[i])
                required_import = compute_import_path(that_file_name, this_file_name)
                # self.console.log(required_import)
                OtherFileMappingApplicator(mappings1, required_import, list(mappings1.keys())).visit(that_ast)
        return ast


class Collector(Transformer, NodeTransformer):
    class _const:
        def to_ast_loader(self):
            return Constant(self.b)

        def __init__(self, b):
            self.b = b

        def __eq__(self, other):
            return hasattr(other, "b") and other.b == self.b

    class _resfunc(_const):
        def to_ast_loader(self):
            return Attribute(
                value=Name(self.owner, Load()),
                attr=self.name,
                ctx=Load()
            )

        def __init__(self, owner, name):
            self.owner = owner
            self.name = name
            super().__init__(f"{owner}.{name}")

    def __init__(self):
        self.in_formatted_str = False
        # self.collect_consts = config["collect_consts"].value
        self.found = []
        super().__init__("collector", "Collects method calls and constants",
                         collect_consts=ConfigValue("Collects constants", True))

    def visit_JoinedStr(self, node: JoinedStr) -> Any:
        self.in_formatted_str = True
        r = self.generic_visit(node)
        self.in_formatted_str = False
        return r

    def visit_FormattedValue(self, node: FormattedValue) -> Any:
        prev = self.in_formatted_str
        self.in_formatted_str = False
        r = self.generic_visit(node)
        self.in_formatted_str = prev
        return r

    def visit_Constant(self, node: Constant) -> Any:
        if self.config["collect_consts"].value:
            val = node.value
            ref = self._const(val)
            if ref in self.found:
                idx = self.found.index(ref)
            else:
                idx = len(self.found)
                self.found.append(ref)
            if self.in_formatted_str:
                return FormattedValue(
                    value=Subscript(
                        value=Name('names', Load()),
                        slice=Constant(idx),
                        ctx=Load()
                    ),
                    conversion=-1
                )
            else:
                return Subscript(
                    value=Name('names', Load()),
                    slice=Constant(idx),
                    ctx=Load()
                )
        return self.generic_visit(node)

    def visit_Call(self, node: Call) -> Any:
        r = self.generic_visit(node)
        if isinstance(node.func, Name) and isinstance(node.func.ctx, Load):
            strified_name = node.func.id
            ref = self._const(strified_name)
            if ref in self.found:  # Already in list, use present index
                idx = self.found.index(ref)
            else:
                idx = len(self.found)
                self.found.append(ref)
            node.func = Call(  # -> eval(names[idx])
                func=Name('eval', Load()),
                args=[
                    Subscript(
                        value=Name('names', ctx=Load()),
                        slice=Constant(idx),
                        ctx=Load()
                    )
                ],
                keywords=[]
            )
        elif isinstance(node.func, Attribute):
            attrib_owner = node.func.value
            # Can only estimate constants as of now, python is fucking weakly typed so i can't estimate return values
            # or similar shit
            # fuck you python
            if isinstance(attrib_owner, Constant):
                const_value = attrib_owner.value
                the_type = type(const_value).__name__
                ref = self._resfunc(the_type, node.func.attr)
                if ref in self.found:
                    idx = self.found.index(ref)
                else:
                    idx = len(self.found)
                    self.found.append(ref)
                node.func = Subscript(
                    value=Name('names', Load()),
                    slice=Constant(idx),
                    ctx=Load()
                )
                node.args.insert(0, attrib_owner)
        return r

    def transform(self, ast: AST, current_file_name, all_asts, all_file_names) -> AST:
        self.found = []
        ast = self.visit(ast)
        new_ast: Module = Module(
            body=[
                Assign(  # names = [x for x in t.found]
                    targets=[
                        Name('names', Store())
                    ],
                    value=List(
                        elts=[x.to_ast_loader() for x in self.found],  # construct string array from found
                        ctx=Load()
                    )
                ),
                *ast.body  # copy old body over
            ],
            type_ignores=[]
        )
        return new_ast


class IntObfuscator(Transformer, NodeTransformer):
    def __init__(self):
        super().__init__("intObfuscator", "Obscures int constants")

    def visit_Constant(self, node: Constant) -> Any:
        s = self.generic_visit(node)
        if type(node.value) == int:
            ic: int = node.value
            is_signed = ic < 0  # signed bit needs to be set only if ic is negative
            rdx = math.ceil((ic.bit_length() + (1 if is_signed else 0)) / 8)  # add said sign bit if the int is signed
            int_bytes = ic.to_bytes(rdx, "little", signed=is_signed)
            off = random.randint(255 + rdx, 999)  # need to keep at least rdx indexes free
            encoded = "".join([format(off - (x + i), "03d") for (x, i) in zip(int_bytes, range(len(int_bytes)))])
            return Call(  # int.from_bytes(..., "little", signed=is_signed)
                func=Attribute(Name('int', Load()), 'from_bytes', Load()),  # int.from_bytes
                args=[
                    Call(  # map(lambda O: 255-int(O), map(''.join, zip(*[iter(encoded)]*3)))
                        func=Name('map', Load()),
                        args=[
                            Lambda(  # lambda O, i: ...
                                args=arguments(
                                    posonlyargs=[],
                                    args=[
                                        arg('O'),
                                        arg('i')
                                    ],
                                    kwonlyargs=[],
                                    kw_defaults=[],
                                    defaults=[]),
                                body=BinOp(  # off - int(O)
                                    left=Constant(value=off),
                                    op=Sub(),
                                    right=BinOp(
                                        left=Call(  # int(O)
                                            func=Name('int', Load()),
                                            args=[
                                                Name('O', Load())],
                                            keywords=[]),
                                        op=Add(),
                                        right=Name('i', Load())
                                    )
                                )),
                            Call(
                                func=Name('map', Load()),
                                args=[
                                    Attribute(
                                        value=Constant(value=''),
                                        attr='join',
                                        ctx=Load()),
                                    Call(
                                        func=Name('zip', Load()),
                                        args=[
                                            Starred(
                                                value=BinOp(
                                                    left=List(
                                                        elts=[
                                                            Call(
                                                                func=Name('iter', Load()),
                                                                args=[
                                                                    Constant(encoded)
                                                                ],
                                                                keywords=[])],
                                                        ctx=Load()),
                                                    op=Mult(),
                                                    right=Constant(value=3)),
                                                ctx=Load())],
                                        keywords=[])],
                                keywords=[]),
                            Call(
                                func=Name('range', Load()),  # range(math.floor(len(encoded)/3))
                                args=[
                                    Constant(math.floor(len(encoded) / 3))
                                ],
                                keywords=[]
                            )
                        ],
                        keywords=[]),
                    Constant(value='little')],
                keywords=[
                    keyword(
                        arg='signed',
                        value=Constant(is_signed)
                    )
                ])
        return s

    def transform(self, ast: AST, current_file_name, all_asts, all_file_names) -> AST:
        return self.visit(ast)


class ReplaceAttribs(Transformer, NodeTransformer):
    def __init__(self):
        super().__init__("replaceAttribSet", "Replaces direct attribute sets with setattr")

    def visit_Assign(self, node: Assign) -> Any:
        if len(node.targets) == 1:
            attrib = node.targets[0]
            if isinstance(attrib, Attribute):
                parent = attrib.value
                name = attrib.attr
                value = node.value
                return Expr(Call(
                    func=Name('setattr', Load()),
                    args=[
                        parent,
                        Constant(name),
                        value
                    ],
                    keywords=[]
                ))
        return self.generic_visit(node)

    def transform(self, ast: AST, current_file_name, all_asts, all_file_names) -> AST:
        return self.visit(ast)


def rnd_name():
    return "".join(random.choices(["l", "I", "M", "N"], k=32))


class ConstructDynamicCodeObject(Transformer):
    _ctype_arg_names = [
        "co_argcount",
        "co_posonlyargcount",
        "co_kwonlyargcount",
        "co_nlocals",
        "co_stacksize",
        ("co_flags", 0),
        "co_code",
        "co_consts",
        "co_names",
        "co_varnames",
        "co_filename",
        ("co_name", ""),
        ("co_qualname", ""),
        ("co_firstlineno", 0),
        ("co_linetable", b""),
        "co_exceptiontable",
        "co_freevars",
        "co_cellvars"
    ]

    def __init__(self):
        self.code_obj_dict = dict()
        super().__init__("dynamicCodeObjLauncher",
                         "Launches the program by constructing it from the ground up with dynamic code objects. This REQUIRES PYTHON 3.11",
                         encrypt=ConfigValue("Encrypts the bytecode with a dynamically generated AES key", True))

    def get_all_code_objects(self, args):
        # args = self.args_from_co(code)
        all_cos = []
        for x in args:
            if isinstance(x, list) or isinstance(x, tuple):
                all_cos = [*self.get_all_code_objects(x), *all_cos]
            elif isinstance(x, CodeType):
                all_cos.append(x)
                all_cos = [*self.get_all_code_objects(self.args_from_co(x)), *all_cos]
        return all_cos

    def args_from_co(self, code: CodeType):
        return [getattr(code, x) if not isinstance(x, tuple) else x[1] for x in self._ctype_arg_names]

    def _parse_const(self, el: Any, ctx):
        if isinstance(el, tuple):
            return Tuple(
                elts=[self._parse_const(x, ctx) for x in el],
                ctx=ctx
            )
        elif isinstance(el, list):
            return List(
                elts=[self._parse_const(x, ctx) for x in el],
                ctx=ctx
            )
        elif isinstance(el, CodeType):
            if el in self.code_obj_dict:  # we have a generator for this, use it
                return Call(
                    func=Name(self.code_obj_dict[el], Load()),
                    args=[],
                    keywords=[]
                )
            else:  # we dont have a generator? alright then, just marshal it
                b = marshal.dumps(el)
                return Call(
                    func=Attribute(
                        value=Call(
                            func=Name('__import__', Load()),
                            args=[
                                Constant("marshal")
                            ],
                            keywords=[]
                        ),
                        attr="loads",
                        ctx=Load()
                    ),
                    args=[
                        Constant(b)
                    ],
                    keywords=[]
                )
        else:
            # if type(el) == bytes:
            #     from util import randomize_cache
            #     list(el)
            #     randomize_cache()
            return Constant(el)

    def create_code_obj_loader(self, func_name: str, compiled_code_obj: CodeType,
                               process_bytecode: Callable[[list], None] = lambda x: randomize_cache(x)) -> FunctionDef:
        collected_args = self.args_from_co(compiled_code_obj)
        co_code_index = self._ctype_arg_names.index("co_code")
        co_code = collected_args[co_code_index]
        co_code_l = list(co_code)

        process_bytecode(co_code_l)

        collected_args[co_code_index] = bytes(co_code_l)
        loader_asm = []
        # value_to_append = dict()
        for i in range(len(collected_args)):  # go over each code object arg
            v = collected_args[i]
            if i > 0 and collected_args[i - 1] == collected_args[i]:  # is the one below us the same as this one?
                elm: list = loader_asm[
                    len(loader_asm) - 1].value.elts  # then expand the assignment to include us aswell
                elm.insert(2, self._parse_const(v, Load()))
                elm[len(elm) - 1].value.slice.lower.value += 1
            else:  # if not, make the assignment
                ass_statement = Assign(  # a = [*a[:i], <arg>, *a[i+1:]] -> insert us at i
                    targets=[Name('a', Store())],
                    value=List(
                        elts=[
                            Starred(Subscript(
                                value=Name('a', Load()),
                                slice=Slice(upper=Constant(i)),
                                ctx=Load()
                            ), Load()),
                            self._parse_const(v, Load()),
                            Starred(Subscript(
                                value=Name('a', Load()),
                                slice=Slice(lower=Constant(i + 1)),
                                ctx=Load()
                            ), Load())
                        ],
                        ctx=Load()
                    )
                )
                # value_to_append[v] = ass_statement
                loader_asm.append(ass_statement)
        random.shuffle(loader_asm)
        finished_asm = FunctionDef(
            name=func_name,
            args=arguments(posonlyargs=[],
                           args=[],
                           kwonlyargs=[],
                           kw_defaults=[],
                           defaults=[]),
            decorator_list=[],
            body=[
                Assign(  # a = []
                    targets=[Name('a', Store())],
                    value=BinOp(
                        left=List(
                            elts=[Constant(None)],
                            ctx=Load()
                        ),
                        op=Mult(),
                        right=Constant(len(collected_args))
                    )
                ),
                *loader_asm,
                Return(
                    Call(
                        func=Call(
                            func=Name('type', Load()),
                            args=[
                                Attribute(
                                    value=Name('b', Load()),
                                    attr='__code__',
                                    ctx=Load()
                                )
                            ],
                            keywords=[]
                        ),
                        args=[
                            Starred(
                                Name('a', Load()),
                                Load()
                            )
                        ],
                        keywords=[]
                    )
                )
            ],
            type_ignores=[]
        )
        return finished_asm

    def do_enc_pass(self, ast_mod: AST) -> Module:
        """
        forgive me
        """
        compiled_code_obj: CodeType = compile(ast_mod, "", "exec", optimize=2)
        dumped = marshal.dumps(compiled_code_obj)
        orig_fnc = FunctionDef(
            name="b",
            args=arguments(posonlyargs=[],
                           args=[],
                           kwonlyargs=[],
                           kw_defaults=[],
                           defaults=[]),
            decorator_list=[],
            body=[
                Expr(
                    Call(
                        func=Name('print', Load()),
                        args=[
                            Constant("what'cha looking for?")
                        ],
                        keywords=[]
                    )
                ),
                *[Assign(
                    targets=[Name(rnd_name(), Store()) for _ in range(random.randint(3, 5))],
                    value=Constant(random.randint(0, 65535))
                ) for _ in range(random.randint(3, 5))]
            ],
            type_ignores=[]
        )
        fix_missing_locations(orig_fnc)
        p = compile(Module(
            body=[orig_fnc],
            type_ignores=[]
        ), "", "exec", optimize=2)
        key = hashlib.md5(
            "".join(map(repr, [p.co_consts[0].co_code, *p.co_consts[0].co_consts, *p.co_consts[0].co_names,
                               *p.co_consts[0].co_varnames])).encode(
                "utf8")).digest()
        aes = AES.new(key, AES.MODE_EAX)
        encrypted = aes.encrypt_and_digest(dumped)
        nonce = aes.nonce
        loader = Module(
            body=[
                ast_import_from("Crypto.Cipher", "AES"),
                Expr(
                    Call(
                        func=Name('exec', Load()),
                        args=[
                            Call(
                                func=Attribute(
                                    value=ast_import_full("marshal"),
                                    attr="loads",
                                    ctx=Load()
                                ),
                                args=[
                                    Call(
                                        func=Attribute(
                                            value=Call(
                                                func=Attribute(
                                                    value=Name("AES", Load()),
                                                    attr="new",
                                                    ctx=Load()
                                                ),
                                                args=[
                                                    Call(
                                                        func=Attribute(
                                                            value=Call(
                                                                func=Attribute(
                                                                    value=ast_import_full("hashlib"),
                                                                    attr="md5",
                                                                    ctx=Load()
                                                                ),
                                                                args=[
                                                                    Call(
                                                                        func=Attribute(
                                                                            value=Call(
                                                                                func=Attribute(
                                                                                    value=Constant(''),
                                                                                    attr='join',
                                                                                    ctx=Load()),
                                                                                args=[
                                                                                    Call(
                                                                                        func=Name('map', Load()),
                                                                                        args=[
                                                                                            Name('repr', Load()),
                                                                                            List(
                                                                                                elts=[
                                                                                                    Attribute(
                                                                                                        Attribute(
                                                                                                            Name(
                                                                                                                'b',
                                                                                                                Load()),
                                                                                                            '__code__',
                                                                                                            Load()),
                                                                                                        'co_code',
                                                                                                        Load()),
                                                                                                    Starred(
                                                                                                        Attribute(
                                                                                                            Attribute(
                                                                                                                Name(
                                                                                                                    'b',
                                                                                                                    Load()),
                                                                                                                '__code__',
                                                                                                                Load()),
                                                                                                            'co_consts',
                                                                                                            Load()),
                                                                                                        Load()),
                                                                                                    Starred(
                                                                                                        Attribute(
                                                                                                            Attribute(
                                                                                                                Name(
                                                                                                                    'b',
                                                                                                                    Load()),
                                                                                                                '__code__',
                                                                                                                Load()),
                                                                                                            'co_names',
                                                                                                            Load()),
                                                                                                        Load()),
                                                                                                    Starred(
                                                                                                        Attribute(
                                                                                                            Attribute(
                                                                                                                Name(
                                                                                                                    'b',
                                                                                                                    Load()),
                                                                                                                '__code__',
                                                                                                                Load()),
                                                                                                            'co_varnames',
                                                                                                            Load()),
                                                                                                        Load())],
                                                                                                ctx=Load())],
                                                                                        keywords=[])],
                                                                                keywords=[]),
                                                                            attr='encode',
                                                                            ctx=Load()),
                                                                        args=[
                                                                            Constant('utf8')],
                                                                        keywords=[])
                                                                ],
                                                                keywords=[]
                                                            ),
                                                            attr="digest",
                                                            ctx=Load()
                                                        ),
                                                        args=[],
                                                        keywords=[]
                                                    ),
                                                    Constant(9),  # MODE_EAX
                                                    Constant(nonce)
                                                ],
                                                keywords=[]
                                            ),
                                            attr="decrypt",
                                            ctx=Load()
                                        ),
                                        args=[
                                            Constant(encrypted[0])
                                        ],
                                        keywords=[]
                                    )
                                ],
                                keywords=[]
                            )
                        ],
                        keywords=[]
                    )
                )
            ],
            type_ignores=[]
        )
        fix_missing_locations(loader)
        compiled_code_obj: CodeType = compile(loader, "", "exec", optimize=2)
        tn = rnd_name()
        main_loader = self.create_code_obj_loader(tn, compiled_code_obj)

        return Module(
            type_ignores=[],
            body=[
                orig_fnc,
                main_loader,
                Expr(Call(
                    func=Name('exec', Load()),
                    args=[
                        Call(
                            func=Name(tn, Load()),
                            args=[],
                            keywords=[]
                        )
                    ],
                    keywords=[]
                ))
            ]
        )

    def transform(self, ast: AST, current_file_name, all_asts, all_file_names) -> AST | Module:
        if sys.version_info[0] < 3 or sys.version_info[1] < 11:
            self.console.log("Python [bold]3.11[/bold] or up is required to use dynamicCodeObjLauncher, skipping", style="red")
            return ast
        ast_mod = fix_missing_locations(ast)
        if self.config["encrypt"].value:
            return self.do_enc_pass(ast_mod)
        else:
            compiled_code_obj: CodeType = compile(ast_mod, bytes([0xDA, 0xAF, 0x1A, 0x87, 0xFF]), "exec", optimize=2)
            all_code_objs = self.get_all_code_objects(self.args_from_co(compiled_code_obj))

            loaders = []
            for x in all_code_objs:  # create names first...
                name = rnd_name()
                self.code_obj_dict[x] = name
            for x in all_code_objs:  # ... then use them
                loaders.append(self.create_code_obj_loader(self.code_obj_dict[x], x))
            main = rnd_name()
            return Module(
                type_ignores=[],
                body=[
                    FunctionDef(
                        name='b',
                        args=arguments(
                            posonlyargs=[],
                            args=[],
                            kwonlyargs=[],
                            kw_defaults=[],
                            defaults=[]),
                        body=[Pass()],
                        decorator_list=[]),
                    *loaders,
                    self.create_code_obj_loader(main, compiled_code_obj),
                    Expr(Call(
                        func=Name('exec', Load()),
                        args=[
                            Call(
                                func=Name(main, Load()),
                                args=[],
                                keywords=[]
                            )
                        ],
                        keywords=[]
                    ))
                ]
            )


class EncodeStrings(Transformer, NodeTransformer):
    def __init__(self):
        self.in_formatted_str = False
        self.no_lzma = False
        super().__init__("encodeStrings", "Encodes strings with base64 and (if not in a fstring) lzma")

    def visit_JoinedStr(self, node: JoinedStr) -> Any:
        self.in_formatted_str = True
        self.no_lzma = True
        r = self.generic_visit(node)
        self.no_lzma = False
        self.in_formatted_str = False
        return r

    def visit_FormattedValue(self, node: FormattedValue) -> Any:
        prev = self.in_formatted_str
        self.in_formatted_str = False
        r = self.generic_visit(node)
        self.in_formatted_str = prev
        return r

    def visit_Constant(self, node: Constant) -> Any:
        val = node.value
        do_decode = False
        if isinstance(val, str):
            encoded = base64.b64encode(val.encode("utf8"))
            do_decode = True
            if self.no_lzma:  # can't use unicode chars in fstrings since these would lead to escapes
                compressed = encoded
            else:
                compressed = zlib.compress(encoded, 9)
        elif type(val) == bytes:
            encoded = base64.b64encode(val)
            if self.no_lzma:
                compressed = encoded
            else:
                compressed = zlib.compress(encoded, 9)
        else:
            compressed = None
        if compressed is not None:
            t = Call(
                func=Attribute(
                    value=ast_import_full("base64"),
                    attr="b64decode",
                    ctx=Load()
                ),
                args=[
                    # We haven't compressed if we're in an fstr
                    Call(
                        func=Attribute(
                            value=ast_import_full("zlib"),
                            attr="decompress",
                            ctx=Load()
                        ),
                        args=[
                            Constant(compressed)
                        ],
                        keywords=[]
                    ) if not self.no_lzma else Constant(compressed)
                ],
                keywords=[]
            )
            if do_decode:
                t = Call(
                    func=Attribute(
                        value=t,
                        attr="decode",
                        ctx=Load()
                    ),
                    args=[],
                    keywords=[]
                )
            if self.in_formatted_str:
                t = FormattedValue(
                    value=t,
                    conversion=-1
                )
            return t
        else:
            return self.generic_visit(node)

    def transform(self, ast: AST, current_file_name, all_asts, all_file_names) -> AST:
        return self.visit(ast)


def collect_fstring_consts(node: JoinedStr) -> str:
    s = ""
    for x in node.values:
        if isinstance(x, Constant):
            s += str(x.value)
        else:
            raise ValueError("Non-constant format specs are not supported")
    return s


class FstringsToFormatSequence(Transformer, NodeTransformer):
    conversion_method_dict = {
        's': "str",
        'r': "repr",
        'a': "ascii"
    }

    def __init__(self):
        super().__init__("fstrToFormatSeq", "Converts F-Strings to their str.format equivalent")

    def visit_JoinedStr(self, node: JoinedStr) -> Any:
        converted_format = ""
        collected_args = []
        for value in node.values:
            if isinstance(value, FormattedValue):
                converted_format += "{"
                if value.format_spec is not None and isinstance(value.format_spec, JoinedStr):  # god i hate fstrings
                    converted_format += ":" + collect_fstring_consts(value.format_spec)
                converted_format += "}"
                loader_mth = value.value
                if value.conversion != -1 and chr(value.conversion) in self.conversion_method_dict:
                    mth = self.conversion_method_dict[chr(value.conversion)]
                    loader_mth = Call(
                        func=Name(mth, Load()),
                        args=[loader_mth],
                        keywords=[]
                    )
                collected_args.append(loader_mth)
            elif isinstance(value, Constant):
                converted_format += str(value.value)
        return Call(
            func=Attribute(
                value=Constant(converted_format),
                attr="format",
                ctx=Load()
            ),
            args=collected_args,
            keywords=[]
        )
        # return self.generic_visit(node)

    def transform(self, ast: AST, current_file_name, all_asts, all_file_names) -> AST:
        return self.visit(ast)
