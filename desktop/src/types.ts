export interface Task {
  video_id: string;
  url: string;
  status: string;
  title_orig: string;
  title_zh: string;
  desc_orig: string;
  desc_zh: string;
  uploader: string;
  work_dir: string;
  video_path: string;
  cover_path: string;
  bv_id: string;
  error: string;
  created_at: string;
  updated_at: string;
  snapshot?: { mode: string; settings: Record<string, unknown> };
  file_exists?: boolean;
  translation?: {
    state?: string;
    provider?: string;
    model_digest?: string;
    fallback_used?: boolean;
    fallback_reason?: string;
    elapsed_ms?: number;
    user_edited?: boolean;
    input_truncated?: boolean;
  };
}
export interface Config {
  work_dir: string;
  bili_tid: number;
  bili_tags: string;
  bili_line: string;
  upload_gap_seconds: number;
  theme: "system" | "dark" | "light";
  hwaccel: string;
  validation_cache: boolean;
  has_deepl_key: boolean;
  data_dir: string;
  youtube_cookies: boolean;
  vault_error: string;
  translation_primary: "local_llm" | "deepl";
  translation_fallback_enabled: boolean;
  translation_ready: boolean;
  translation_upgrade_notice?: boolean;
  local_llm_mode: "managed" | "external";
  local_llm_base_url: string;
  local_llm_model: string;
}
export interface Progress {
  stage: string;
  percent?: number | null;
  speed?: number;
  eta?: number;
  remaining?: number;
  track?: string;
  backend?: string;
  provider?: string;
}
export const labels: Record<string, string> = {
  pending: "等待处理",
  fetching_meta: "读取信息",
  queued_download: "等待下载",
  downloading: "下载中",
  queued_validation: "等待校验",
  validating: "校验中",
  queued_upload: "等待准备 / 投稿",
  processing_cover: "处理封面",
  translating: "翻译中",
  ready: "待预览",
  uploading: "投稿中",
  submitted: "已提交",
  failed: "失败",
  cancelled: "已取消",
  cancel_requested: "正在取消",
  interrupted: "已中断",
  submission_unknown: "待核对",
  hashing: "读取文件指纹",
  upload_wait: "等待上传间隔",
  translation_wait: "等待翻译服务",
  translation_fallback: "切换翻译服务",
};
export const editable = (task: Task) => task.status === "ready";
export const retryable = (task: Task) =>
  ["failed", "cancelled", "interrupted"].includes(task.status);
export const active = (task: Task) =>
  ![
    "ready",
    "submitted",
    "failed",
    "cancelled",
    "interrupted",
    "submission_unknown",
  ].includes(task.status);
export function bytes(value: number) {
  return value >= 1024 ** 3
    ? `${(value / 1024 ** 3).toFixed(1)} GB`
    : `${(value / 1024 ** 2).toFixed(1)} MB`;
}
