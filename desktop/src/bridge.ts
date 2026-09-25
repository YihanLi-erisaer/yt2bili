import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open, save } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";

export const preview =
  import.meta.env.DEV &&
  !isTauri() &&
  new URLSearchParams(location.search).has("preview");
export async function request<T = any>(
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  if (preview) return (await import("./preview")).request(method, params) as T;
  if (!isTauri())
    throw new Error(
      "请使用 npm run desktop 启动桌面应用。此页面尚未连接本地后台。",
    );
  const result = await invoke<T>("backend_request", { method, params });
  if (method === "system.health") {
    await invoke("frontend_ready", { health: result });
  }
  return result;
}
export async function subscribe(
  callback: (event: { event: string; payload: any }) => void,
) {
  if (!isTauri()) return () => {};
  const stop = await listen<{ event: string; payload: any }>(
    "backend-event",
    (e) => callback(e.payload),
  );
  const offline = await listen("backend-disconnected", () =>
    callback({ event: "disconnected", payload: {} }),
  );
  return () => {
    stop();
    offline();
  };
}
export async function onClose(callback: () => void) {
  return isTauri() ? listen("app-close-requested", callback) : () => {};
}
export const closeApp = () => invoke("finish_close");
export async function chooseFile(extensions = ["txt"]) {
  if (!isTauri()) throw new Error("文件选择需要在桌面应用中使用。");
  return open({
    multiple: false,
    filters: [{ name: "支持的文件", extensions }],
  }) as Promise<string | null>;
}
export async function chooseDirectory() {
  if (!isTauri()) throw new Error("目录选择需要在桌面应用中使用。");
  return open({ directory: true, multiple: false }) as Promise<string | null>;
}
export async function saveLog() {
  return save({
    defaultPath: "yt2bili-diagnostics.txt",
    filters: [{ name: "诊断日志", extensions: ["txt"] }],
  });
}
export async function external(url: string) {
  if (
    !/^https:\/\/(www\.bilibili\.com|member\.bilibili\.com|stardazz-com\.vercel\.app)\//.test(
      url,
    )
  )
    return;
  if (isTauri()) await openUrl(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}
export const operationId = () => crypto.randomUUID();
