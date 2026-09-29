import asyncio
import json
from typing import Annotated, Literal

import pytest
from pydantic import Field

from server_agent.tools.registry import ToolError, ToolRegistry, clean_schema, truncate


def make_registry():
    reg = ToolRegistry()

    @reg.tool
    def search_log(
        path: Annotated[str, Field(description="日志路径")],
        level: Annotated[Literal["error", "warn"], Field(description="级别")] = "error",
        limit: Annotated[int, Field(ge=1, le=100, description="条数")] = 10,
        keyword: Annotated[str | None, Field(description="关键字")] = None,
    ) -> dict:
        """在日志中搜索。"""
        return {"path": path, "level": level, "limit": limit, "keyword": keyword}

    @reg.tool(name="boom", max_chars=50)
    def _boom(kind: str) -> str:
        """故意出错或输出很长的工具。"""
        if kind == "tool_error":
            raise ToolError("文件不存在: /x")
        if kind == "crash":
            return 1 / 0
        return "x" * 200

    @reg.tool
    async def async_echo(text: str) -> str:
        """异步工具。"""
        await asyncio.sleep(0)
        return text

    return reg


def test_schema_generated_from_signature():
    fn = make_registry().get("search_log").schema()
    assert fn["type"] == "function" and fn["function"]["description"] == "在日志中搜索。"
    params = fn["function"]["parameters"]
    assert params["required"] == ["path"]
    assert params["additionalProperties"] is False
    props = params["properties"]
    assert props["path"] == {"type": "string", "description": "日志路径"}
    assert props["level"]["enum"] == ["error", "warn"] and props["level"]["default"] == "error"
    assert props["limit"]["minimum"] == 1 and props["limit"]["maximum"] == 100
    assert props["keyword"]["type"] == "string" and "anyOf" not in props["keyword"]  # Optional 被压平
    assert "title" not in json.dumps(params)  # 自动生成的 title 已去掉


def test_clean_schema_keeps_property_named_title():
    s = {"type": "object", "title": "M", "properties": {"title": {"type": "string", "title": "Title"}}}
    assert clean_schema(s) == {"type": "object", "properties": {"title": {"type": "string"}}}


def test_registration_rules():
    reg = ToolRegistry()
    with pytest.raises(ValueError, match="docstring"):
        @reg.tool
        def no_doc(x: int) -> int:
            return x
    with pytest.raises(TypeError, match="类型注解"):
        @reg.tool
        def no_hint(x):
            """d"""
    with pytest.raises(TypeError, match="kwargs"):
        @reg.tool
        def var_kw(**kw: int):
            """d"""

    @reg.tool
    def dup() -> int:
        """d"""
        return 1
    def another() -> int:
        """d"""
        return 2

    with pytest.raises(ValueError, match="重名"):
        reg.tool(name="dup")(another)


async def test_call_ok_with_dict_and_json_string():
    reg = make_registry()
    r = await reg.call("search_log", {"path": "/var/log/a"})
    assert r.ok and r.data == {"path": "/var/log/a", "level": "error", "limit": 10, "keyword": None}
    assert json.loads(r.content)["limit"] == 10
    r2 = await reg.call("search_log", '{"path": "/a", "limit": "5"}')  # 宽松模式：字符串数字可转换
    assert r2.ok and r2.data["limit"] == 5


@pytest.mark.parametrize(
    "args, needle",
    [
        ({}, "path: Field required"),
        ({"path": "/a", "limit": 0}, "limit"),
        ({"path": "/a", "level": "debug"}, "level"),
        ({"path": "/a", "force": True}, "force: Extra inputs are not permitted"),
        ("{bad json", "不是合法 JSON"),
        ("[1, 2]", "JSON 对象"),
    ],
)
async def test_call_bad_arguments_become_readable_errors(args, needle):
    r = await make_registry().call("search_log", args)
    assert not r.ok and needle in r.error
    assert json.loads(r.content) == {"error": r.error}  # 回喂模型的是 JSON 错误


async def test_unknown_tool_lists_available():
    r = await make_registry().call("rm_rf", {})
    assert not r.ok and "search_log" in r.error


async def test_tool_error_crash_truncate_async():
    reg = make_registry()
    r = await reg.call("boom", {"kind": "tool_error"})
    assert not r.ok and r.error == "文件不存在: /x"
    r = await reg.call("boom", {"kind": "crash"})
    assert not r.ok and r.error.startswith("ZeroDivisionError")
    r = await reg.call("boom", {"kind": "long"})
    assert r.ok and r.truncated and r.content.startswith("x" * 50) and "已截断" in r.content
    r = await reg.call("async_echo", {"text": "hi"})
    assert r.ok and r.content == "hi"


async def test_timeout():
    reg = ToolRegistry()

    @reg.tool(timeout=0.05)
    async def slow() -> str:
        """慢工具。"""
        await asyncio.sleep(1)
        return "done"

    r = await reg.call("slow")
    assert not r.ok and "超时" in r.error


def test_truncate():
    assert truncate("abc", 5) == ("abc", False)
    text, cut = truncate("abcdef", 3)
    assert cut and text.startswith("abc") and "原始 6 字符" in text
