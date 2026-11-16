# DeepSeek 联网搜索

参考 [DeepSeek 联网工具文档](https://api-docs.deepseek.com/zh-cn/guides/web_tools/)，使用 Chat Completions API 的服务端工具 `deepseek_web_search`。

在管理后台将 DeepSeek 的模型改为 `deepseek-flash`，或在 `data/.config.yaml` 中配置：

```yaml
LLM:
  DeepSeekLLM:
    type: openai
    model_name: deepseek-flash
    url: https://api.deepseek.com
    api_key: 你的 API Key
    web_search: true
```

重启服务后生效。已有自定义配置中的模型名优先于默认配置，需要手动更新。旧模型继续正常对话，但不启用此联网工具。

官方域名默认开启联网搜索；`web_search: false` 可关闭。通过代理访问时，需显式设置 `web_search: true`，并确认代理支持该服务端工具。

普通对话和函数调用对话都会声明搜索工具，设备原有的 function 工具继续可用。模型自行决定是否联网，搜索及结果拼接均在 DeepSeek 服务端完成，无须另配搜索 Key。搜索事件不交给设备执行，语音与聊天继续输出回答正文，不播报搜索过程或引用元数据。

默认沿用语音对话的非思考模式。需要深度思考搜索时，在同一条目下添加 `extra_body: {thinking: {type: enabled}}`；搜索声明与思考开关独立。

可以询问“请联网查询今天的科技新闻并总结”，检查回答及 DeepSeek 平台用量。是否触发搜索仍由模型决定；工具调用按官方规则另行计费。
