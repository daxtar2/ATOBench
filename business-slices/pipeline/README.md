# pipeline/

通用批量环境构建工具（**产物默认不进 git**）。

```bash
# 校验公开 / 本地 spec
python3 business-slices/pipeline/cli.py validate \
  business-slices/fixtures/tiny/tiny-ticket-flow.build.json

# 生成到 business-slices/generated/（已 gitignore）
python3 business-slices/pipeline/cli.py build-one \
  business-slices/fixtures/tiny/tiny-ticket-flow.build.json --force

# 批量：目录内所有 *.json
python3 business-slices/pipeline/cli.py build-batch \
  business-slices/fixtures/tiny --force
```

私有原料放 `business-slices/_private/`（gitignore），BuildSpec 里用：

```json
"source": { "kind": "private_trace_ref", "private_ref": "my-trace.json" }
```

仓库只提交：`pipeline/`、`schemas/`、`fixtures/tiny/`、`templates/`、本说明。  
详见 [../PIPELINE.md](../PIPELINE.md)。
