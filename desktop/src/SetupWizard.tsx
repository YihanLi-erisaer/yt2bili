import { useState } from "react";
import { ArrowRight, CheckCircle2, FolderOpen, RefreshCw } from "lucide-react";
import { chooseDirectory, request } from "./bridge";
import { bytes, type Config } from "./types";

export default function SetupWizard({
  config,
  auth,
  diagnostics,
  busy,
  action,
  refresh,
  setDiagnostics,
  done,
}: {
  config: Config;
  auth: any;
  diagnostics: any;
  busy: boolean;
  action: (work: () => Promise<unknown>, success?: string) => Promise<void>;
  refresh: () => Promise<void>;
  setDiagnostics: (value: any) => void;
  done: () => void;
}) {
  const [step, setStep] = useState(0);
  const [folder, setFolder] = useState(config.work_dir);
  const [key, setKey] = useState("");
  const [checkedKey, setCheckedKey] = useState(false);
  const labels = ["素材目录", "运行环境", "翻译服务", "投稿账号"];
  const readyTools =
    diagnostics?.tools
      .filter((t: any) => ["ffmpeg", "ffprobe", "biliup"].includes(t.name))
      .every((t: any) => t.available) &&
    diagnostics?.tools.some(
      (t: any) => ["node", "deno"].includes(t.name) && t.available,
    );
  return (
    <div className="wizard-body">
      <ol className="wizard-steps">
        {labels.map((label, i) => (
          <li key={label} className={i === step ? "current" : ""}>
            <span>{i < step ? <CheckCircle2 size={15} /> : i + 1}</span>
            {label}
          </li>
        ))}
      </ol>
      <h3>{labels[step]}</h3>
      {step === 0 && (
        <>
          <p className="help">
            选择保存视频和封面的文件夹。任务数据库和账号配置会独立保存在本机用户目录。
          </p>
          <label className="field-label" htmlFor="wizard-folder">
            素材工作目录
          </label>
          <div className="button-row">
            <input
              id="wizard-folder"
              value={folder}
              onChange={(e) => setFolder(e.target.value)}
            />
            <button
              className="secondary"
              disabled={busy}
              onClick={() =>
                action(async () => {
                  const path = await chooseDirectory();
                  if (path) setFolder(path);
                })
              }
            >
              <FolderOpen size={16} />
              选择
            </button>
          </div>
        </>
      )}
      {step === 1 && (
        <>
          <p className="help">
            下载和处理视频需要下列工具。Node.js 与 Deno
            至少提供一个；缺失时请按开发说明安装。
          </p>
          <div className="wizard-tools">
            {diagnostics?.tools.map((tool: any) => (
              <div key={tool.name}>
                <strong>{tool.name}</strong>
                <span>{tool.available ? tool.version : "未就绪"}</span>
              </div>
            ))}
          </div>
          <p className="help">
            {diagnostics
              ? `可用空间 ${bytes(diagnostics.free_bytes)}`
              : "正在检测环境…"}
          </p>
          <button
            className="secondary"
            disabled={busy}
            onClick={() =>
              action(async () =>
                setDiagnostics(await request("system.diagnostics")),
              )
            }
          >
            <RefreshCw size={15} />
            重新检测
          </button>
        </>
      )}
      {step === 2 && (
        <>
          <p className="help">
            DeepL 密钥保存在系统凭据存储中。保存并检测后继续。
          </p>
          <label className="field-label" htmlFor="wizard-key">
            DeepL API 密钥
          </label>
          <input
            id="wizard-key"
            type="password"
            autoComplete="off"
            placeholder={
              config.has_deepl_key
                ? "已保存；留空沿用现有密钥"
                : "输入 DeepL API 密钥"
            }
            value={key}
            onChange={(e) => {
              setKey(e.target.value);
              setCheckedKey(false);
            }}
          />
          <button
            className="secondary"
            disabled={busy || (!key && !config.has_deepl_key)}
            onClick={() =>
              action(async () => {
                if (key) {
                  await request("credentials.set", { value: key });
                  setKey("");
                }
                await request("credentials.test");
                setCheckedKey(true);
                await refresh();
              }, "DeepL 连接正常。")
            }
          >
            保存并检测连接
          </button>
          {checkedKey && <p className="help">连接正常，可以继续。</p>}
        </>
      )}
      {step === 3 && (
        <>
          <p className="help">
            使用哔哩哔哩 App
            扫码。也可以稍后在“账号与连接”登录，先使用素材预览功能。
          </p>
          {auth.login?.qrcode && (
            <img
              className="wizard-qr"
              src={auth.login.qrcode}
              alt="B 站登录二维码"
            />
          )}
          <p className="help">
            {auth.configured
              ? "账号已连接"
              : (
                  {
                    loading: "正在获取二维码…",
                    waiting: "请扫码并在手机上确认",
                    scanned: "请在手机上确认",
                    expired: "二维码已过期，请刷新",
                    failed: auth.login?.message || "登录失败，请重试",
                  } as Record<string, string>
                )[auth.login?.status] || "尚未连接账号"}
          </p>
          <button
            className="secondary"
            disabled={busy || auth.login?.status === "loading"}
            onClick={() =>
              action(async () => {
                await request("auth.login.start");
                await refresh();
              })
            }
          >
            {auth.login?.qrcode ? "刷新二维码" : "扫码登录"}
          </button>
        </>
      )}
      <div className="modal-actions">
        {step > 0 && (
          <button
            className="secondary"
            disabled={busy}
            onClick={() =>
              action(async () => {
                if (step === 3) await request("auth.login.cancel");
                setStep(step - 1);
              })
            }
          >
            上一步
          </button>
        )}
        <button
          className="primary"
          disabled={
            busy || (step === 1 && !readyTools) || (step === 2 && !checkedKey)
          }
          onClick={() =>
            action(async () => {
              if (step === 0) {
                await request("settings.update", {
                  values: { work_dir: folder },
                });
                await refresh();
                setDiagnostics(await request("system.diagnostics"));
              }
              if (step === 3) {
                await request("auth.login.cancel");
                await refresh();
                done();
              } else setStep(step + 1);
            })
          }
        >
          {step === 3 ? "完成配置" : "下一步"}
          <ArrowRight size={15} />
        </button>
      </div>
    </div>
  );
}
