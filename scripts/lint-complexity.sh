#!/bin/bash
# lint-complexity.sh — 复杂度治理检查
# 检查 core 文件的新增函数/方法是否带有 ponytail: 注释或对应删除了等量旧代码。
# 在 pre-commit 或 CI 中使用。
#
# 用法: scripts/lint-complexity.sh [--strict]
#   --strict: 失败时 exit 1（CI 模式），默认只 warning

CORE_FILES=("wecom_server/router.py" "scheduler.py" "machine.py" "directives.py")
STRICT=false
HAS_WARNINGS=false

for arg in "$@"; do
    [ "$arg" = "--strict" ] && STRICT=true
done

RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

for f in "${CORE_FILES[@]}"; do
    [ ! -f "$f" ] && continue

    # 只检查已暂存的变更（pre-commit 场景）
    additions=$(git diff --cached -- "$f" 2>/dev/null)
    [ -z "$additions" ] && continue

    # 提取新增的 def/class 行
    new_defs=$(echo "$additions" | grep '^+' | grep -E '^\+\s*(def |class )' | sed 's/^+//' | sed 's/(.*//')
    [ -z "$new_defs" ] && continue

    # 检查新增中是否有 ponytail: 注释
    has_ponytail=$(echo "$additions" | grep -c 'ponytail:')
    # 计算删减行数
    removed_lines=$(echo "$additions" | grep '^-' | grep -cvE '^---')
    added_lines=$(echo "$additions" | grep '^+' | grep -cvE '^\+\+\+')

    if [ "$has_ponytail" -eq 0 ] && [ "$added_lines" -gt "$removed_lines" ]; then
        echo -e "${YELLOW}⚠️  $f: 新增了函数/类，但缺少 ponytail: 注释说明为什么不能用已有机制实现${NC}"
        echo "$new_defs" | sed 's/^/  新增: /'
        echo "  建议: 加一行 # ponytail: <不能复用已有机制的原因>"
        echo "        或删掉等量的旧代码来抵消新增"
        HAS_WARNINGS=true
    fi

    # 检查新增中引用已有模式的场景
    echo "$new_defs" | while IFS= read -r def_line; do
        [ -z "$def_line" ] && continue
        # 找函数名
        func_name=$(echo "$def_line" | awk '{print $2}')
        # 看新增的 diff 里有没有 ponytail 在函数附近
        context=$(git diff --cached -U3 -- "$f" 2>/dev/null | grep -A5 "^+.*def $func_name")
        if ! echo "$context" | grep -q 'ponytail:'; then
            :
        fi
    done
done

if $HAS_WARNINGS; then
    echo ""
    echo "ponytail: 注释格式:"
    echo "  # ponytail: STATUS_TABLE 无法表达此状态转换，因为决策依赖外部输入"
    echo "  # ponytail: 已有 _pending_gray_drafts 只能查询，这里需要写入操作"
    if $STRICT; then
        exit 1
    fi
fi
exit 0