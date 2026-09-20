// Explicit development-only preview. Never used by the native desktop transport.
import type { Task } from "./types";
const config = {
  work_dir: "D:\\yt2bili-work",
  bili_tid: 171,
  bili_tags: "转载",
  bili_line: "tx",
  upload_gap_seconds: 20,
  theme: "dark",
  hwaccel: "auto",
  validation_cache: true,
  has_deepl_key: false,
  data_dir: "本地应用数据目录",
  youtube_cookies: false,
  vault_error: "",
};
let tasks: Task[] = [];
if (new URLSearchParams(location.search).has("populated"))
  tasks = [
    {
      video_id: "abcdefghijk",
      title_zh: "用更少的工具，构建更专注的工作流",
      title_orig: "A focused creative workflow",
      status: "ready",
      uploader: "Studio Notes",
    },
    {
      video_id: "lmnopqrstuv",
      title_zh: "关于设计系统，我们学到了什么",
      title_orig: "Lessons from a design system",
      status: "validating",
      uploader: "Design Journal",
    },
    {
      video_id: "wxyz1234567",
      title_zh: "让创作回归简单",
      title_orig: "Making things simpler",
      status: "submitted",
      uploader: "Creative Process",
    },
  ].map((t) => ({
    ...t,
    url: `https://www.youtube.com/watch?v=${t.video_id}`,
    desc_orig: "An exploration of thoughtful tools and creative work.",
    desc_zh: "探索更简单的工具与创作方式。",
    work_dir: "",
    video_path: "",
    cover_path: "",
    bv_id: t.status === "submitted" ? "BV1234567890" : "",
    error: "",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }));
export async function request(method: string, params: any): Promise<any> {
  if (method === "system.health") return { protocol_version: 1 };
  if (method === "settings.get") return { ...config };
  if (method === "settings.update") {
    Object.assign(config, params.values);
    return { ...config };
  }
  if (method === "auth.status")
    return { configured: false, login: { status: "idle" } };
  if (method === "auth.login.cancel") return { cancelled: true };
  if (method === "system.diagnostics")
    return {
      tools: ["ffmpeg", "ffprobe", "biliup", "node"].map((name) => ({
        name,
        available: true,
        version: "开发预览 · 未执行检测",
        path: "",
      })),
      free_bytes: 128 * 1024 ** 3,
    };
  if (method === "tasks.list") {
    const items = tasks.filter(
      (t) =>
        (!params.history || t.status === "submitted") &&
        (!params.status || params.status === t.status) &&
        (!params.search || t.title_zh.includes(params.search)),
    );
    return {
      items,
      total: items.length,
      counts: {
        ready: tasks.filter((t) => t.status === "ready").length,
        validating: tasks.filter((t) => t.status === "validating").length,
      },
      queue: { active: [] },
    };
  }
  if (method === "tasks.get")
    return tasks.find((t) => t.video_id === params.video_id);
  if (method === "tasks.cover") return { image: null };
  if (method === "logs.tail") return { items: [] };
  if (method === "tasks.update_metadata") {
    const task = tasks.find((t) => t.video_id === params.video_id)!;
    task.title_zh = params.title;
    task.desc_zh = params.description;
    return task;
  }
  if (method === "tasks.create")
    throw new Error("当前为界面预览，请在桌面应用中创建真实任务。");
  throw new Error("该操作需要真实桌面服务，预览不会调用外部账号。");
}
