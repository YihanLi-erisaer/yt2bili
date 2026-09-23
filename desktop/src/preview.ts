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
  translation_primary: "local_llm",
  translation_fallback_enabled: true,
  translation_ready: false,
  local_llm_mode: "managed",
  local_llm_base_url: "http://127.0.0.1:11435",
  local_llm_model: "qwen3:8b",
  data_dir: "本地应用数据目录",
  youtube_cookies: false,
  vault_error: "",
};
const number = Math.min(
  5,
  Math.max(
    0,
    Number(new URLSearchParams(location.search).get("accounts") || 0),
  ),
);
const accounts = Array.from({ length: number }, (_, i) => ({
  account_id: `account-${i + 1}`,
  uid: String(10001 + i),
  nickname: `账号 ${i + 1}`,
  remark: "",
  lifecycle: "active",
  slot: i + 1,
  auth_state: "valid",
}));
let localInstalled = false;
const translationJobs: any[] = [];
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
    task_id: t.video_id,
    account_id: accounts[0]?.account_id || null,
    account_uid_snapshot: accounts[0]?.uid || null,
    account_name_snapshot: accounts[0]?.nickname || "",
    revision: 1,
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
if (new URLSearchParams(location.search).has("samevideo") && tasks.length)
  tasks = accounts.map((a) => ({
    ...tasks[0],
    task_id: a.account_id + "-samevideo",
    account_id: a.account_id,
    account_uid_snapshot: a.uid,
    account_name_snapshot: a.nickname,
  }));
export async function request(method: string, params: any): Promise<any> {
  if (method === "system.health") return { protocol_version: 2 };
  if (method === "translation.status")
    return {
      local: {
        state: localInstalled ? "ready" : "missing",
        message: localInstalled
          ? "开发预览 · 本地组件已就绪"
          : "开发预览 · 尚未安装本地组件",
      },
    };
  if (method === "translation.jobs.get")
    return params.job_id
      ? translationJobs.find((j) => j.job_id === params.job_id)
      : { items: translationJobs };
  if (method === "translation.install" || method === "translation.test") {
    const job = {
      job_id: crypto.randomUUID(),
      kind: method.endsWith("test") ? "test:" + params.provider : "install",
      state: "running",
      result: {
        title: "更好的工作流",
        description: "构建实用工具。保留版本 2.0。",
        elapsed_ms: 800,
      },
    };
    translationJobs.push(job);
    setTimeout(() => {
      if (job.state === "running") {
        job.state = "complete";
        if (job.kind === "install") localInstalled = true;
        config.translation_ready = true;
      }
    }, 1000);
    return { job_id: job.job_id };
  }
  if (method === "translation.jobs.cancel") {
    translationJobs.find((j) => j.job_id === params.job_id).state = "cancelled";
    return { requested: true };
  }
  if (method === "tasks.retranslate") {
    const task = tasks.find((t) => t.task_id === params.task_id)!;
    task.title_zh = "重新翻译的标题";
    return { queued: true };
  }

  if (method === "settings.get") return { ...config };
  if (method === "settings.update") {
    Object.assign(config, params.values);
    return { ...config };
  }
  if (method === "auth.status")
    return {
      accounts,
      configured: accounts.length > 0,
      login: { status: "idle" },
    };
  if (method === "accounts.list") return { items: accounts, limit: 5 };
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
        (!params.account_id || t.account_id === params.account_id) &&
        (!params.history || t.status === "submitted") &&
        (!params.status || params.status === t.status) &&
        (!params.search || t.title_zh.includes(params.search)),
    );
    return {
      items,
      total: items.length,
      all_total: tasks.length,
      counts: {
        ready: tasks.filter((t) => t.status === "ready").length,
        validating: tasks.filter((t) => t.status === "validating").length,
      },
      queue: {
        active: [],
        download: {},
        validate: {},
        uploads: accounts.map((a) => ({
          account_id: a.account_id,
          queued_count: 0,
        })),
      },
    };
  }
  if (method === "tasks.get")
    return tasks.find((t) => t.task_id === params.task_id);
  if (method === "tasks.cover") return { image: null };
  if (method === "logs.tail") return { items: [] };
  if (method === "tasks.update_metadata") {
    const task = tasks.find((t) => t.task_id === params.task_id)!;
    task.revision += 1;
    task.title_zh = params.title;
    task.desc_zh = params.description;
    return task;
  }
  if (method === "tasks.create")
    throw new Error("当前为界面预览，请在桌面应用中创建真实任务。");
  throw new Error("该操作需要真实桌面服务，预览不会调用外部账号。");
}
