import { useEffect, useState } from "react";
import { operationId, request } from "./bridge";
import { labels, type Task, type Publication } from "./types";

export function DouyinAccountPanel() {
  const [status, setStatus] = useState<any>(null);
  const [url, setUrl] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = async (verify = false) =>
    setStatus(await request("douyin.auth.status", { verify }));
  useEffect(() => {
    void refresh().catch((e) => setError(String(e)));
  }, []);
  const run = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await work();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="section-body">
      <h3>抖音同步投稿 · 单账号</h3>
      <p className="help">
        复用同一份视频、封面和翻译结果，抖音独立排队。需部署官方授权服务并取得发布权限。
      </p>
      {error && <p className="inline-error">{error}</p>}
      {status?.error && <p className="inline-error">{status.error}</p>}
      <p>
        {status?.account
          ? `${status.account.nickname} · ${status.account.auth_state === "valid" ? "已登录" : "需重新授权"}`
          : "尚未绑定抖音账号"}
      </p>
      {!status?.account && (
        <>
          <label className="field">
            HTTPS 授权服务地址
            <input
              value={url}
              type="url"
              placeholder="https://douyin.example.com"
              onChange={(e) => setUrl(e.target.value)}
            />
          </label>
          <label className="field">
            服务配对密钥
            <input
              value={key}
              type="password"
              autoComplete="off"
              onChange={(e) => setKey(e.target.value)}
            />
          </label>
          <button
            disabled={busy || !url || !key}
            onClick={() =>
              void run(async () => {
                await request("douyin.auth.configure", {
                  url,
                  pairing_key: key,
                });
                setKey("");
              })
            }
          >
            保存服务连接
          </button>
        </>
      )}
      <div className="modal-actions">
        <button
          disabled={busy || !status?.configured}
          onClick={() => void run(() => request("douyin.auth.start"))}
        >
          在浏览器授权抖音
        </button>
        <button
          disabled={busy || !status?.configured}
          onClick={() =>
            void run(async () => {
              const next = await request("douyin.auth.status", {
                verify: true,
              });
              if (next.error) throw new Error(next.error);
            })
          }
        >
          检查授权状态
        </button>
        <button
          disabled={busy || !status?.configured}
          onClick={() => void run(() => request("douyin.auth.cancel"))}
        >
          取消本次授权
        </button>
        {status?.account && (
          <>
            <button
              disabled={busy}
              onClick={() => {
                if (
                  window.confirm(
                    "请确认已排除抖音限流原因。恢复等待中的抖音队列？",
                  )
                )
                  void run(() => request("douyin.accounts.resume_uploads"));
              }}
            >
              恢复抖音上传队列
            </button>
            <button
              disabled={busy}
              onClick={() => void run(() => request("douyin.auth.clear"))}
            >
              退出登录
            </button>
            <button
              disabled={busy}
              onClick={() => {
                if (
                  window.confirm(
                    "归档抖音账号后才能绑定其他账号。未完成投稿必须先处理，历史记录保留。继续？",
                  )
                )
                  void run(() => request("douyin.accounts.archive"));
              }}
            >
              归档并更换账号
            </button>
          </>
        )}
      </div>
      <p className="help">
        授权完成后点击“检查授权状态”。退出登录不会释放账号名额，也不会改变已有任务的目标账号。
      </p>
    </section>
  );
}

function PublicationCard({
  pub,
  busy,
  run,
  onDirtyChange,
}: {
  pub: Publication;
  busy: boolean;
  run: (work: () => Promise<unknown>) => Promise<void>;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [text, setText] = useState(pub.text);
  const [remote, setRemote] = useState("");
  useEffect(() => setText(pub.text), [pub.text, pub.publication_id]);
  useEffect(() => {
    if (pub.platform === "douyin") onDirtyChange(text !== pub.text);
  }, [text, pub.text, pub.platform, onDirtyChange]);
  const call = (method: string, extra = {}) =>
    run(() =>
      request(method, { publication_id: pub.publication_id, ...extra }),
    );
  return (
    <div className="section-body">
      <strong>
        {pub.platform === "douyin" ? "抖音" : "Bilibili"} ·{" "}
        {labels[pub.status] || pub.status}
      </strong>
      <p className="help">
        目标账号：{pub.account_label || pub.account_id}{" "}
        {pub.remote_id && ` · 作品 ID：${pub.remote_id}`}
      </p>
      {pub.error && <p className="inline-error">{pub.error}</p>}
      {pub.platform === "douyin" && (
        <label className="field">
          抖音文案（独立于 B 站简介）
          <textarea
            value={text}
            maxLength={1000}
            disabled={busy || pub.status !== "ready"}
            onChange={(e) => setText(e.target.value)}
          />
          {pub.status === "ready" && (
            <button
              disabled={busy || text === pub.text || !text.trim()}
              onClick={() =>
                void call("publications.update_metadata", {
                  text,
                  revision: pub.revision,
                })
              }
            >
              保存抖音文案
            </button>
          )}
        </label>
      )}
      <div className="modal-actions">
        {["failed", "cancelled", "interrupted", "blocked_validation"].includes(
          pub.status,
        ) && (
          <button
            disabled={busy}
            onClick={() =>
              void call("publications.retry", { operation_id: operationId() })
            }
          >
            继续此目标并预览
          </button>
        )}
        {["queued", "waiting", "ready"].includes(pub.status) && (
          <button
            disabled={busy}
            onClick={() => void call("publications.cancel")}
          >
            取消此目标
          </button>
        )}
        {[
          "ready",
          "failed",
          "cancelled",
          "interrupted",
          "blocked_validation",
        ].includes(pub.status) && (
          <button
            disabled={busy}
            onClick={() => {
              if (
                window.confirm(
                  "明确放弃此投稿目标？其他目标完成后可能清理共享素材。",
                )
              )
                void call("publications.abandon");
            }}
          >
            放弃此目标
          </button>
        )}
      </div>
      {pub.status === "submission_unknown" && (
        <>
          <p className="help">
            先核对对应账号的创作中心。上传完成不代表作品已创建；不明结果不会自动重发。
          </p>
          <input
            aria-label="核对后的作品 ID"
            value={remote}
            onChange={(e) => setRemote(e.target.value)}
            placeholder="核对后的作品 ID"
          />
          <button
            disabled={busy || !remote.trim()}
            onClick={() => {
              if (window.confirm("确认此作品属于本次投稿？"))
                void call("publications.resolve", { remote_id: remote });
            }}
          >
            登记已提交
          </button>
          <button
            disabled={busy}
            onClick={() => {
              if (
                window.confirm(
                  "确认平台上没有此作品？错误确认可能造成重复投稿。",
                )
              )
                void call("publications.resolve", { not_submitted: true });
            }}
          >
            确认未提交
          </button>
        </>
      )}
    </div>
  );
}

export function PublicationDetails({
  task,
  busy,
  action,
  refresh,
  dirty,
  onDirtyChange,
}: {
  task: Task;
  busy: boolean;
  action: (work: () => Promise<unknown>) => Promise<void>;
  refresh: () => Promise<void>;
  dirty: boolean;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const run = (work: () => Promise<unknown>) =>
    action(async () => {
      await work();
      await refresh();
    });
  return (
    <section>
      <h3>各平台投稿结果</h3>
      {task.publications?.map((pub) => (
        <PublicationCard
          key={pub.publication_id}
          pub={pub}
          busy={busy}
          run={run}
          onDirtyChange={onDirtyChange}
        />
      ))}
      {task.status === "partial_success" &&
        task.publications?.some((p) => p.status === "ready") && (
          <button
            disabled={busy || dirty}
            onClick={() => {
              if (
                window.confirm(
                  "仅提交尚未成功且已准备好的目标？已成功平台不会重复投稿。",
                )
              )
                void run(() =>
                  request("tasks.submit", {
                    task_id: task.task_id,
                    operation_id: operationId(),
                    targets: task.publications
                      ?.filter((p) => p.status === "ready")
                      .map((p) => p.publication_id),
                    revisions: Object.fromEntries(
                      task.publications!.map((p) => [
                        p.publication_id,
                        p.revision,
                      ]),
                    ),
                  }),
                );
            }}
          >
            确认投稿未完成目标
          </button>
        )}
    </section>
  );
}
