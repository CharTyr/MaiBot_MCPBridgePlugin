#!/usr/bin/env python3
"""
变量替换/路径解析测试脚本

用于验证工具链变量替换支持:
- list 下标访问: return.0 / return[0]
- bracket key 访问: ['return']
"""

import json

from tool_chain import ToolChainExecutor


def main() -> None:
    executor = ToolChainExecutor(mcp_manager=None)

    geo_result = {
        "return": [
            {
                "country": "中国",
                "province": "湖南省",
                "city": "娄底市",
                "location": "114.301,30.576",
                "adcode": "420106",
            }
        ]
    }

    context = {
        "input": {"q": "x"},
        "step": {"geo": json.dumps(geo_result, ensure_ascii=False)},
        "prev": json.dumps([{"id": 1}, {"id": 2}], ensure_ascii=False),
    }

    assert executor._get_var_value("step.geo.return.0.location", context) == "114.301,30.576"
    assert executor._get_var_value("step.geo.return[0].location", context) == "114.301,30.576"
    assert executor._get_var_value("step.geo['return'][0]['location']", context) == "114.301,30.576"
    assert executor._get_var_value("prev.1.id", context) == "2"
    assert executor._get_var_value("prev[1].id", context) == "2"
    assert executor._get_var_value("step.geo.return.9.location", context) == ""
    assert executor._get_var_value("step.geo.not_exist", context) == ""

    print("✅ tool_chain var path tests passed")


if __name__ == "__main__":
    main()

