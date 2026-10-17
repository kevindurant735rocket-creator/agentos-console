# findings（子代理并行体检摘要）

结构：tools/102py + gates.sh70门，README内链47过期，以gates.sh为准。无Dockerfile/CI/rollback。
--check 140/140绿，tools/仅agent_core.py默认full脏改。

自进化：evo沙箱只跑2门，distill/promote无执行体，提案无schema。选DEFAULT_GATES加安全门最小补。
GitHub：is_readonly放行auth token，api_get直连，写面动态拼接只剩运行时拦。选去token最小补。
监督：steer游标非原子，watch吞错，边界消费可绕。选commit原子写最小补。
规划：dsp不查环，ledger静默，审批无撤销。选validate加环检查+unapprove最小补。
