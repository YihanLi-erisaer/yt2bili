import { useEffect, useState } from "react";
import { request } from "./bridge";

export function AcfunAccountPanel() {
  const [status, setStatus] = useState<any>(null);
  const [qr, setQr] = useState("");
  const [phase, setPhase] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = async (verify = false) => setStatus(await request("acfun.auth.status", { verify }));
  useEffect(() => { void refresh().catch((e) => setError(String(e))); }, []);
  useEffect(() => {
    if (!qr) return;
    const id = window.setInterval(() => {
      void request("acfun.auth.poll").then((value) => {
        setPhase(value.status);
        if (["done", "expired", "failed"].includes(value.status)) {
          setQr("");
          void refresh();
        }
      }).catch((e) => { setError(String(e)); setQr(""); });
    }, 2500);
    return () => window.clearInterval(id);
  }, [qr]);
  const run = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); await refresh(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };
  return <section className="section-body">
    <h3>AcFun 同步投稿 · 单账号</h3>
    <p className="help">实验性网页接入，独立队列。平台接口尚需真实账号验证；启用前请确认你接受网页接口变化的风险。</p>
    {error && <p className="inline-error">{error}</p>}
    {status?.error && <p className="help">{status.error}</p>}
    <p>{status?.account ? `${status.account.nickname} · UID ${status.account.user_id} · ${status.account.auth_state === "valid" ? "已登录" : "需重新扫码"}` : "尚未绑定 AcFun 账号"}</p>
    <label className="checkbox"><input type="checkbox" checked={!!status?.enabled} disabled={busy}
      onChange={(e) => void run(() => request("settings.update", { values: { acfun_experimental_enabled: e.target.checked } }))} />启用 AcFun 实验性接入</label>
    <div className="modal-actions">
      <button disabled={busy} onClick={() => void run(async () => {
        const result = await request("acfun.auth.start");
        setQr(result.qrcode); setPhase(result.status);
      })}>扫码登录 AcFun</button>
      <button disabled={busy || !qr} onClick={() => void run(async () => {
        await request("acfun.auth.cancel"); setQr(""); setPhase("");
      })}>取消扫码</button>
      {status?.account && <>
        <button disabled={busy} onClick={() => void run(() => request("acfun.auth.status", { verify: true }))}>检查登录状态</button>
        <button disabled={busy} onClick={() => void run(() => request("acfun.accounts.resume_uploads"))}>恢复 AcFun 队列</button>
        <button disabled={busy} onClick={() => void run(() => request("acfun.auth.clear"))}>退出登录</button>
        <button disabled={busy} onClick={() => { if (window.confirm("归档 AcFun 账号并保留历史投稿记录？")) void run(() => request("acfun.accounts.archive")); }}>归档账号</button>
      </>}
    </div>
    {qr && <div><img alt="AcFun 登录二维码" width={220} height={220} src={qr.startsWith("data:") ? qr : `data:image/png;base64,${qr}`} /><p className="help">{phase === "scanned" ? "已扫码，请在手机确认。" : "请用 AcFun 扫码并在手机确认。"}</p></div>}
    {phase === "expired" && <p className="inline-error">二维码已过期，请重新获取。</p>}
  </section>;
}
