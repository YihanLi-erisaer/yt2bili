import { useEffect, useState } from "react";
import { request, operationId, chooseFile } from "./bridge";
import type { Config } from "./types";

type Action = (work: () => Promise<unknown>, message?: string) => Promise<void>;
type Job = {
  job_id: string;
  kind: string;
  state: string;
  message?: string;
  progress?: { stage: string; percent?: number | null };
  result?: {
    title?: string;
    description?: string;
    elapsed_ms?: number;
    message?: string;
  };
};
const running = (job: Job | null) =>
  !!job && ["running", "cancel_requested"].includes(job.state);
const phase: Record<string, string> = {
  runtime_download: "下载运行时",
  model_download: "下载模型",
  model_verify: "校验模型",
  translation_wait: "等待翻译服务",
  translating: "正在试译",
  translation_fallback: "切换服务",
};

export default function TranslationPanel({
  config,
  busy,
  action,
  refresh,
  onReady,
}: {
  config: Config;
  busy: boolean;
  action: Action;
  refresh: () => Promise<void>;
  onReady?: (ready: boolean) => void;
}) {
  const [key, setKey] = useState("");
  const [address, setAddress] = useState(config.local_llm_base_url);
  const [localTimeout, setLocalTimeout] = useState(
    String(config.local_llm_timeout_seconds),
  );
  const [totalTimeout, setTotalTimeout] = useState(
    String(config.translation_total_timeout_seconds),
  );
  const [local, setLocal] = useState<{ state: string; message: string } | null>(
    null,
  );
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const blocked = busy || running(job);
  const readStatus = async () =>
    setLocal((await request("translation.status")).local);
  useEffect(() => {
    setAddress(config.local_llm_base_url);
    setLocalTimeout(String(config.local_llm_timeout_seconds));
    setTotalTimeout(String(config.translation_total_timeout_seconds));
    onReady?.(false);
    void readStatus().catch((e) => setError(String(e)));
  }, [
    config.local_llm_mode,
    config.local_llm_base_url,
    config.translation_primary,
    config.translation_fallback_enabled,
    config.local_llm_timeout_seconds,
    config.translation_total_timeout_seconds,
  ]);
  useEffect(() => {
    let alive = true;
    void request("translation.jobs.get")
      .then((value) => {
        if (alive) setJob(value.items.find((j: Job) => running(j)) || null);
      })
      .catch((e) => {
        if (alive) setError(String(e));
      });
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    if (!running(job)) return;
    let alive = true;
    const timer = setInterval(() => {
      void request<Job>("translation.jobs.get", { job_id: job!.job_id })
        .then(async (value) => {
          if (!alive) return;
          setJob(value);
          if (!running(value)) {
            clearInterval(timer);
            await refresh();
            await readStatus();
            if (value.state === "complete" && value.kind.startsWith("test:")) {
              const provider = value.kind.slice(5);
              onReady?.(
                provider === config.translation_primary ||
                  config.translation_fallback_enabled,
              );
            }
          }
        })
        .catch((e) => {
          if (alive) setError(String(e));
        });
    }, 700);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [job?.job_id, job?.state]);
  const save = (values: Record<string, unknown>) =>
    action(async () => {
      onReady?.(false);
      await request("settings.update", { values });
      await refresh();
    }, "翻译设置已保存，新任务生效。");
  const saveTimeouts = () => {
    const localSeconds = Number(localTimeout);
    const totalSeconds = Number(totalTimeout);
    if (
      !Number.isInteger(localSeconds) ||
      localSeconds < 15 ||
      localSeconds > 600
    ) {
      setError("大模型推理超时必须是 15～600 秒之间的整数。");
      return;
    }
    if (
      !Number.isInteger(totalSeconds) ||
      totalSeconds < 30 ||
      totalSeconds > 1200
    ) {
      setError("翻译流程总超时必须是 30～1200 秒之间的整数。");
      return;
    }
    if (totalSeconds <= localSeconds) {
      setError("翻译流程总超时必须大于大模型推理超时。");
      return;
    }
    setError("");
    void save({
      local_llm_timeout_seconds: localSeconds,
      translation_total_timeout_seconds: totalSeconds,
    });
  };
  const start = (method: string, params: Record<string, unknown> = {}) =>
    action(async () => {
      setError("");
      onReady?.(false);
      const value = await request(method, {
        ...params,
        operation_id: operationId(),
      });
      setJob(await request("translation.jobs.get", { job_id: value.job_id }));
    });
  return (
    <section className="settings-card translation-panel">
      <div className="section-title">
        <div>
          <h2>翻译服务</h2>
          <p>默认本地推理，也可优先使用 DeepL。</p>
        </div>
      </div>
      <div className="section-body">
        {config.translation_upgrade_notice && (
          <p className="help">
            升级后，新任务默认本地优先；未安装本地模型时会按开关尝试
            DeepL。旧任务保留原策略，保存设置后关闭此提示。
          </p>
        )}
        <label className="field">
          首选翻译服务
          <select
            aria-label="首选翻译服务"
            value={config.translation_primary}
            disabled={blocked}
            onChange={(e) => void save({ translation_primary: e.target.value })}
          >
            <option value="local_llm">本地大模型（默认）</option>
            <option value="deepl">DeepL</option>
          </select>
        </label>
        <label className="translation-toggle">
          <input
            type="checkbox"
            checked={config.translation_fallback_enabled}
            disabled={blocked}
            onChange={(e) =>
              void save({ translation_fallback_enabled: e.target.checked })
            }
          />
          首选失败时使用另一服务
        </label>
        <p className="help">
          {config.translation_primary === "local_llm" ? "本地大模型" : "DeepL"}
          {config.translation_fallback_enabled
            ? ` → ${config.translation_primary === "local_llm" ? "DeepL" : "本地大模型"}`
            : " · 不自动切换"}
          。 使用 DeepL 时，标题和简介会发送至
          DeepL。仅本地使用时请关闭自动切换。
        </p>
        <h3>本地大模型 · Qwen3 8B</h3>
        <label className="field">
          本地运行方式
          <select
            aria-label="本地运行方式"
            value={config.local_llm_mode}
            disabled={blocked}
            onChange={(e) =>
              void save({
                local_llm_mode: e.target.value,
                local_llm_base_url:
                  e.target.value === "managed"
                    ? "http://127.0.0.1:11435"
                    : "http://127.0.0.1:11434",
              })
            }
          >
            <option value="managed">应用管理（Windows x64）</option>
            <option value="external">连接已有本机 Ollama</option>
          </select>
        </label>
        {config.local_llm_mode === "external" && (
          <div className="inline-controls">
            <input
              aria-label="本地服务地址"
              value={address}
              disabled={blocked}
              onChange={(e) => setAddress(e.target.value)}
            />
            <button
              className="secondary"
              disabled={blocked}
              onClick={() => void save({ local_llm_base_url: address })}
            >
              保存地址
            </button>
          </div>
        )}
        <p role="status">{local?.message || "正在检测组件…"}</p>
        <p className="help">
          首次下载约 6.7 GB；安装时需预留约 12 GB 空间。推荐 16 GB
          以上内存，实际速度取决于硬件。安装后可断网翻译，首次请试译验证。
        </p>
        <h3>翻译超时</h3>
        <div className="form-grid">
          <label className="field">
            大模型推理超时（秒）
            <input
              aria-label="大模型推理超时（秒）"
              type="number"
              min={15}
              max={600}
              step={1}
              value={localTimeout}
              disabled={blocked}
              onChange={(e) => setLocalTimeout(e.target.value)}
            />
          </label>
          <label className="field">
            翻译流程总超时（秒）
            <input
              aria-label="翻译流程总超时（秒）"
              type="number"
              min={30}
              max={1200}
              step={1}
              value={totalTimeout}
              disabled={blocked}
              onChange={(e) => setTotalTimeout(e.target.value)}
            />
          </label>
        </div>
        <div className="button-row">
          <p className="help">
            推理超时为单次本地模型请求上限；流程总超时涵盖重试和备用服务切换，且必须更长。新设置只影响新任务。
          </p>
          <button
            className="secondary"
            disabled={blocked}
            onClick={saveTimeouts}
          >
            保存超时设置
          </button>
        </div>
        <div className="button-row">
          {config.local_llm_mode === "managed" && (
            <>
              <button
                className="secondary"
                disabled={blocked}
                onClick={() => void start("translation.install")}
              >
                安装 / 重试下载
              </button>
              <button
                className="text-button"
                disabled={blocked}
                onClick={() =>
                  void action(async () => {
                    const file = await chooseFile(["zip"]);
                    if (file)
                      await start("translation.install", {
                        offline_path: file,
                      });
                  })
                }
              >
                导入离线组件包
              </button>
            </>
          )}
          <button
            className="secondary"
            disabled={blocked}
            onClick={() =>
              void start("translation.test", { provider: "local_llm" })
            }
          >
            本地试译
          </button>
          <button
            className="text-button"
            disabled={blocked}
            onClick={() => void action(readStatus)}
          >
            重新检测
          </button>
        </div>
        <h3>DeepL · 可选</h3>
        <label className="field">
          DeepL API 密钥
          <input
            type="password"
            autoComplete="off"
            disabled={blocked}
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={
              config.has_deepl_key
                ? "已安全保存，输入新密钥可替换"
                : "使用本地模型无需填写"
            }
          />
        </label>
        <div className="button-row">
          <button
            className="secondary"
            disabled={blocked || !key.trim()}
            onClick={() =>
              void action(async () => {
                await request("credentials.set", { value: key });
                setKey("");
                await refresh();
              }, "密钥已保存到系统凭据存储。")
            }
          >
            保存密钥
          </button>
          <button
            className="secondary"
            disabled={blocked || !config.has_deepl_key}
            onClick={() =>
              void start("translation.test", { provider: "deepl" })
            }
          >
            DeepL 试译
          </button>
          <button
            className="text-button"
            disabled={blocked || !config.has_deepl_key}
            onClick={() =>
              void action(async () => {
                await request("credentials.set", { value: "" });
                onReady?.(false);
                await refresh();
              }, "DeepL 密钥已清除。")
            }
          >
            清除密钥
          </button>
        </div>
        <p className="help">
          密钥只保存在系统凭据存储；DeepL 试译会消耗少量额度。
        </p>
        {config.vault_error && (
          <p className="inline-error">
            {config.vault_error} 本地翻译仍可使用。
          </p>
        )}
        {job && (
          <div className="translation-job" role="status">
            <strong>
              {
                (
                  {
                    running: "操作中",
                    cancel_requested: "正在取消",
                    complete: "已完成",
                    failed: "操作失败",
                    cancelled: "已取消",
                    interrupted: "已中断",
                  } as Record<string, string>
                )[job.state]
              }
            </strong>
            {running(job) && (
              <p>
                {phase[job.progress?.stage || ""] || "正在准备组件…"}
                {job.progress?.percent != null
                  ? ` · ${job.progress.percent.toFixed(1)}%`
                  : ""}
              </p>
            )}
            {job.message && <p>{job.message}</p>}
            {job.result?.message && <p>{job.result.message}</p>}
            {job.result?.title && (
              <p>
                {job.result.title}
                <br />
                {job.result.description}
                <br />
                耗时 {((job.result.elapsed_ms || 0) / 1000).toFixed(1)} 秒
              </p>
            )}
            {running(job) && (
              <button
                className="secondary"
                disabled={job.state === "cancel_requested"}
                onClick={() =>
                  void action(async () => {
                    await request("translation.jobs.cancel", {
                      job_id: job.job_id,
                    });
                    setJob(
                      await request("translation.jobs.get", {
                        job_id: job.job_id,
                      }),
                    );
                  })
                }
              >
                取消操作
              </button>
            )}
          </div>
        )}
        {error && <p className="inline-error">{error}</p>}
      </div>
    </section>
  );
}
