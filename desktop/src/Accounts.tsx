import { useState } from "react";
import { chooseFile, request } from "./bridge";
import { accountLabel, type BiliAccount } from "./types";

export function AccountSelector({
  accounts,
  value,
  onChange,
  archived = false,
}: {
  accounts: BiliAccount[];
  value: string;
  onChange: (id: string) => void;
  archived?: boolean;
}) {
  return (
    <label className="field">
      目标 Bilibili 账号
      <select
        aria-label="目标 Bilibili 账号"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">请选择一个账号</option>
        {accounts
          .filter((a) => archived || a.lifecycle === "active")
          .map((a) => (
            <option key={a.account_id} value={a.account_id}>
              {accountLabel(a)}
              {a.lifecycle === "archived" ? "（已归档）" : ""}
            </option>
          ))}
      </select>
    </label>
  );
}

export function AccountsPanel({
  auth,
  refresh,
}: {
  auth: any;
  refresh: () => Promise<void>;
}) {
  const accounts: BiliAccount[] = auth.accounts || [];
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");
  const [login, setLogin] = useState<{
    account_id?: string;
    session_id: string;
  } | null>(null);
  const [rename, setRename] = useState("");
  const [remark, setRemark] = useState("");
  const run = async (id: string, work: () => Promise<unknown>) => {
    setPending((p) => ({ ...p, [id]: true }));
    setError("");
    try {
      await work();
      await refresh();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setPending((p) => ({ ...p, [id]: false }));
    }
  };
  const start = (account_id?: string) =>
    run(account_id || "new", async () => {
      const result = await request("auth.login.start", { account_id });
      setLogin({ account_id, session_id: result.session_id });
    });
  const importFile = (account_id?: string) =>
    run(account_id || "new", async () => {
      const path = await chooseFile(["json"]);
      if (path)
        await request("auth.import", { kind: "bilibili", path, account_id });
    });
  const status =
    auth.login?.session_id === login?.session_id
      ? auth.login
      : { status: "loading" };
  const labels: Record<string, string> = {
    valid: "登录有效",
    unverified: "待验证",
    expired: "登录失效",
    missing: "未登录",
    unavailable: "验证暂不可用",
  };
  return (
    <section className="settings-card">
      <div className="section-title">
        <div>
          <h2>哔哩哔哩账号 · {accounts.length}/5</h2>
          <p>每个账号使用独立上传队列；凭据保存在本机。</p>
        </div>
      </div>
      <div className="section-body">
        {error && (
          <div className="inline-error" role="alert">
            {error}
          </div>
        )}
        {accounts.map((a) => (
          <div className="account-row" key={a.account_id}>
            <strong>{accountLabel(a)}</strong>
            <span className="pill">{labels[a.auth_state] || a.auth_state}</span>
            <div className="button-row">
              <button
                disabled={pending[a.account_id]}
                onClick={() => start(a.account_id)}
              >
                重新登录
              </button>
              <button
                disabled={pending[a.account_id]}
                onClick={() => importFile(a.account_id)}
              >
                导入凭据
              </button>
              <button
                disabled={pending[a.account_id]}
                onClick={() =>
                  run(a.account_id, () =>
                    request("auth.renew", { account_id: a.account_id }),
                  )
                }
              >
                续期
              </button>
              <button
                disabled={pending[a.account_id]}
                onClick={() =>
                  run(a.account_id, () =>
                    request("accounts.verify", { account_id: a.account_id }),
                  )
                }
              >
                验证
              </button>
              <button
                onClick={() => {
                  setRename(a.account_id);
                  setRemark(a.remark);
                }}
              >
                备注
              </button>
              <button
                disabled={pending[a.account_id]}
                onClick={() =>
                  run(a.account_id, async () => {
                    if (
                      confirm(
                        `清除 ${accountLabel(a)} 的登录凭据？任务和账号仍保留。`,
                      )
                    )
                      await request("auth.clear", {
                        kind: "bilibili",
                        account_id: a.account_id,
                      });
                  })
                }
              >
                退出登录
              </button>
              <button
                disabled={pending[a.account_id]}
                onClick={() =>
                  run(a.account_id, async () => {
                    if (
                      confirm(
                        `归档 ${accountLabel(a)}？只有全部任务已提交时才能释放名额。`,
                      )
                    )
                      await request("accounts.archive", {
                        account_id: a.account_id,
                      });
                  })
                }
              >
                归档
              </button>
              <button
                disabled={pending[a.account_id]}
                onClick={() =>
                  run(a.account_id, async () => {
                    if (
                      confirm(
                        `请先核对 ${accountLabel(a)} 的创作中心，并确认没有遗留上传进程。恢复该账号上传队列？`,
                      )
                    )
                      await request("accounts.resume_uploads", {
                        account_id: a.account_id,
                        confirmed_no_upload: true,
                      });
                  })
                }
              >
                恢复上传
              </button>
            </div>
            {rename === a.account_id && (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void run(a.account_id, async () => {
                    await request("accounts.rename", {
                      account_id: a.account_id,
                      remark,
                    });
                    setRename("");
                  });
                }}
              >
                <input
                  aria-label="账号备注"
                  maxLength={80}
                  value={remark}
                  onChange={(e) => setRemark(e.target.value)}
                />
                <button>保存备注</button>
              </form>
            )}
          </div>
        ))}
        <div className="button-row">
          <button
            className="primary"
            disabled={accounts.length >= 5 || pending.new}
            onClick={() => start()}
          >
            添加账号
          </button>
          <button
            className="secondary"
            disabled={accounts.length >= 5 || pending.new}
            onClick={() => importFile()}
          >
            导入新账号
          </button>
        </div>
        {!accounts.length && (
          <p className="help">
            先添加账号，再新建任务。重新添加已归档 UID 会恢复原账号身份。
          </p>
        )}
        {login && (
          <div className="qr-panel" role="region" aria-label="账号登录">
            {status.qrcode && <img src={status.qrcode} alt="B 站登录二维码" />}
            <p>
              {status.message ||
                (
                  {
                    loading: "正在获取二维码",
                    waiting: "请扫码并确认目标 UID",
                    scanned: "请在手机确认",
                    success: "登录成功",
                    expired: "二维码已过期",
                    failed: "登录失败",
                  } as Record<string, string>
                )[status.status]}
            </p>
            <button onClick={() => start(login.account_id)}>刷新二维码</button>
            <button
              onClick={() => {
                void request("auth.login.cancel", {
                  session_id: login.session_id,
                });
                setLogin(null);
              }}
            >
              关闭二维码
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

export function QueueOverview({
  queue,
  accounts,
}: {
  queue: any;
  accounts: BiliAccount[];
}) {
  const waits: Record<string, string> = {
    rate_limited: "限流暂停，需恢复",
    auth_required: "等待重新登录",
    auth_unverified: "等待验证",
    account_busy: "等待账号锁 / 核对",
    cooldown: "上传间隔",
    queue: "排队中",
  };
  return (
    <div className="queue-overview" aria-label="队列概览">
      {[
        { label: "共享下载", ...queue?.download },
        { label: "共享校验", ...queue?.validate },
        { label: "共享封面 / 翻译", ...queue?.prepare },
        { label: "抖音独立上传", ...queue?.douyin },
        { label: "AcFun 独立上传", ...queue?.acfun },
        ...(queue?.uploads || []).map((q: any) => ({
          ...q,
          label: accounts.find((a) => a.account_id === q.account_id)
            ? accountLabel(accounts.find((a) => a.account_id === q.account_id)!)
            : q.account_id,
        })),
      ].map((q: any) => (
        <div className="queue-card" key={q.label}>
          <strong>{q.label}</strong>
          <small>
            {q.running_task_id ? "执行中" : waits[q.wait_reason] || "空闲"} ·
            等待 {q.queued_count || 0}
            {q.remaining_seconds
              ? ` · ${Math.ceil(q.remaining_seconds)} 秒`
              : ""}
          </small>
        </div>
      ))}
    </div>
  );
}
