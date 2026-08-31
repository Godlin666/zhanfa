#!/usr/bin/env bash
# 一次性配置阿里云 OSS 数据分发。可重复执行（幂等）。
#
# 做四件事：
#   1. 建私有 Bucket
#   2. 建 uploader / reader 两个最小权限子账号，生成密钥
#   3. 密钥写进本地 600 文件 + GitHub Secrets（终端不打印）
#   4. 再建一个只有 OSS 权限的 zhanfa-admin，写进 CLI profile，
#      好让你能安全删掉主账号 AccessKey
#
# 前置：aliyun CLI 已 configure、gh 已登录、阿里云账户有余额且已实名。
#
#   用法：BUCKET=你的桶名 bash setup_oss.sh
set -euo pipefail

REGION="${REGION:-cn-hangzhou}"
BUCKET="${BUCKET:-}"          # 不留默认值：桶名不进公开仓库
REPO="${REPO:-Godlin666/zhanfa}"
ENDPOINT="oss-${REGION}.aliyuncs.com"
CRED="$HOME/zhanfa-oss-credentials.txt"

say() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
note(){ printf '  · %s\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -n "$BUCKET" ] || die "请指定桶名：BUCKET=你的桶名 bash setup_oss.sh"
command -v aliyun >/dev/null || die "未安装 aliyun CLI：brew install aliyun-cli"
command -v gh >/dev/null     || die "未安装 gh CLI：brew install gh"

# 从 CreateAccessKey 的返回里取出 id 和 secret，不落到任何日志
getak() {
  python3 -c "import sys,json;d=json.load(sys.stdin)['AccessKey'];print(d['AccessKeyId']+' '+d['AccessKeySecret'])"
}

# 阿里云每个子账号最多 2 把 AK；重复执行时先清掉旧的，避免撞上限。
# 注意用 for 而不是 `| while read`：后者在没有密钥可删时，
# 循环体最后一条 [ -n "$k" ] 返回 1 会成为整个管道的退出码，撞上 set -e 直接静默退出。
purge_aks() {
  local user="$1" k ids
  ids=$(aliyun ram ListAccessKeys --UserName "$user" 2>/dev/null \
        | python3 -c "import sys,json;print(' '.join(x['AccessKeyId'] for x in json.load(sys.stdin)['AccessKeys']['AccessKey']))" 2>/dev/null) || return 0
  for k in $ids; do
    aliyun ram DeleteAccessKey --UserName "$user" --UserAccessKeyId "$k" >/dev/null 2>&1 \
      && note "已清理 $user 的旧密钥 ${k:0:8}***"
  done
  return 0
}

say "0/5 预检"
BAL=$(aliyun bssopenapi QueryAccountBalance 2>/dev/null \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['Data']['AvailableAmount'])" 2>/dev/null || echo "?")
note "账户可用余额: ${BAL} 元"

say "1/5 创建 Bucket（私有读写）：$BUCKET @ $REGION"
# 用退出码判断，不要用「输出里有没有 error 字样」——那会假阳性，
# 导致桶没建成却继续往下生成密钥
if aliyun oss ls "oss://$BUCKET" >/dev/null 2>&1; then
  note "Bucket 已存在，跳过"
elif aliyun oss mb "oss://$BUCKET" --acl private -e "$ENDPOINT" >/dev/null 2>&1; then
  ok "Bucket 已创建"
else
  die "建桶失败（UserDisable / 403）。账号未实名认证或欠费时 OSS 不允许建桶：
     · 实名认证： https://account.console.aliyun.com/v2/#/authc/home
     · 充值：     https://usercenter2.aliyun.com/finance/fund-management/recharge
     处理完重新执行本脚本即可（脚本可重复运行）。"
fi

say "2/5 创建权限策略"
cat > /tmp/zhanfa-uploader-policy.json <<JSON
{"Version":"1","Statement":[{"Effect":"Allow",
 "Action":["oss:PutObject","oss:PutObjectAcl","oss:GetObject","oss:DeleteObject",
           "oss:ListObjects","oss:AbortMultipartUpload","oss:ListParts"],
 "Resource":["acs:oss:*:*:${BUCKET}","acs:oss:*:*:${BUCKET}/*"]}]}
JSON
cat > /tmp/zhanfa-reader-policy.json <<JSON
{"Version":"1","Statement":[{"Effect":"Allow",
 "Action":["oss:GetObject","oss:ListObjects"],
 "Resource":["acs:oss:*:*:${BUCKET}","acs:oss:*:*:${BUCKET}/v1/*"]}]}
JSON
for role in uploader reader; do
  name="zhanfa-${role}"
  if aliyun ram CreatePolicy --PolicyName "$name" \
       --PolicyDocument "$(cat /tmp/zhanfa-${role}-policy.json)" \
       --Description "zhanfa 数据分发 - ${role}" >/dev/null 2>&1; then
    ok "策略 $name"
  else
    note "策略 $name 已存在"
  fi
done

say "3/5 创建子账号并授权"
for role in uploader reader; do
  name="zhanfa-${role}"
  aliyun ram CreateUser --UserName "$name" >/dev/null 2>&1 && ok "用户 $name" || note "用户 $name 已存在"
  aliyun ram AttachPolicyToUser --PolicyType Custom --PolicyName "$name" --UserName "$name" \
    >/dev/null 2>&1 && ok "授权 $name" || note "$name 已授权"
done

say "4/5 生成密钥 → 本地文件 + GitHub Secrets（终端不打印）"
PREFIX=$(python3 -c 'import secrets;print(secrets.token_hex(16))')
purge_aks zhanfa-uploader
purge_aks zhanfa-reader
UP_LINE=$(aliyun ram CreateAccessKey --UserName zhanfa-uploader | getak) \
  || die "生成 uploader 密钥失败（子账号最多 2 把密钥，可能已达上限）"
RD_LINE=$(aliyun ram CreateAccessKey --UserName zhanfa-reader | getak) \
  || die "生成 reader 密钥失败"
read -r UP_ID UP_SEC <<< "$UP_LINE"
read -r RD_ID RD_SEC <<< "$RD_LINE"
[ -n "$UP_ID" ] && [ -n "$RD_ID" ] || die "密钥解析为空，请重试"
ok "两把密钥已生成"

umask 077
cat > "$CRED" <<TXT
# zhanfa 数据分发凭证   生成于 $(date '+%F %T')
# 此文件权限 600。不要贴进任何聊天窗口、issue 或截图。

【发给老婆的 —— 只读，只能取 v1/ 下的数据，改不了删不了】
  Region          : ${REGION}
  Endpoint        : ${ENDPOINT}
  Bucket          : ${BUCKET}
  AccessKeyId     : ${RD_ID}
  AccessKeySecret : ${RD_SEC}

  免密码直读地址（浏览器点开就下 / pandas 一行直读）：
  https://${BUCKET}.${ENDPOINT}/p/${PREFIX}/meta.json
  https://${BUCKET}.${ENDPOINT}/p/${PREFIX}/hk_recent90.zip
  https://${BUCKET}.${ENDPOINT}/p/${PREFIX}/us_recent90.zip
  https://${BUCKET}.${ENDPOINT}/p/${PREFIX}/symbols.csv

  取数方法见项目里的 OPENDATA.md

【GitHub Actions 用 —— 上传权限，已自动写入仓库 Secrets】
  AccessKeyId     : ${UP_ID}
  AccessKeySecret : ${UP_SEC}
  PublicPrefix    : ${PREFIX}
TXT
chmod 600 "$CRED"
ok "$CRED (600)"

for kv in "OSS_ENDPOINT=$ENDPOINT" "OSS_BUCKET=$BUCKET" "OSS_KEY_ID=$UP_ID" \
          "OSS_KEY_SECRET=$UP_SEC" "OSS_PUBLIC_PREFIX=$PREFIX"; do
  gh secret set "${kv%%=*}" -R "$REPO" -b "${kv#*=}" >/dev/null \
    && ok "Secret ${kv%%=*}" || die "写入 Secret ${kv%%=*} 失败，检查 gh auth status"
done

say "5/5 建一个只有 OSS 权限的账号，好让你能删掉主账号密钥"
aliyun ram CreateUser --UserName zhanfa-admin >/dev/null 2>&1 && ok "用户 zhanfa-admin" || note "已存在"
aliyun ram AttachPolicyToUser --PolicyType System --PolicyName AliyunOSSFullAccess \
  --UserName zhanfa-admin >/dev/null 2>&1 && ok "授权 AliyunOSSFullAccess（只能碰 OSS）" || note "已授权"
purge_aks zhanfa-admin
AD_LINE=$(aliyun ram CreateAccessKey --UserName zhanfa-admin | getak) || die "生成 zhanfa-admin 密钥失败"
read -r AD_ID AD_SEC <<< "$AD_LINE"
aliyun configure set --profile zhanfa --mode AK --access-key-id "$AD_ID" \
  --access-key-secret "$AD_SEC" --region "$REGION" >/dev/null 2>&1 \
  || aliyun configure set --profile zhanfa --mode AK --access-key-id "$AD_ID" \
       --access-key-secret "$AD_SEC" --region-id "$REGION" >/dev/null 2>&1
ok "已写入 CLI profile 'zhanfa'"
printf '\n【zhanfa-admin —— 仅 OSS 权限，已写入 CLI profile "zhanfa"】\n  AccessKeyId     : %s\n  AccessKeySecret : %s\n' \
  "$AD_ID" "$AD_SEC" >> "$CRED"

rm -f /tmp/zhanfa-*-policy.json

cat <<TXT

════════════════════════════════════════════════════════

  Bucket    : ${BUCKET}
  Endpoint  : ${ENDPOINT}
  公开前缀  : p/${PREFIX}/

  凭证在这里（终端没打印，自己看）：
      cat ${CRED}

  ⚠️ 接下来请立刻做：

  1. 删掉你的主账号 AccessKey（它已在对话里明文出现过）
       https://ram.console.aliyun.com/manage/ak
     删掉之后我这边只剩 zhanfa-admin 的 OSS 权限，
     碰不到你的账单、ECS、RAM。

  2. 回来告诉我，我接着做上传验证。

  3. 全部验证完，把 ${CRED} 里【发给老婆的】那段
     连同 OPENDATA.md 一起发给她，然后可以删掉该文件。

════════════════════════════════════════════════════════
TXT
