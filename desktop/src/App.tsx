import {
  createContext,
  useContext,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  ExternalLink,
  FileText,
  FolderOpen,
  History,
  KeyRound,
  Link2,
  ListVideo,
  Loader2,
  Monitor,
  Moon,
  Plus,
  RefreshCw,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  Sun,
  Upload,
  Video,
  X,
  Zap,
} from "lucide-react";
import {
  request,
  subscribe,
  preview,
  chooseFile,
  chooseDirectory,
  saveLog,
  external,
  operationId,
  onClose,
  closeApp,
} from "./bridge";
import {
  active,
  bytes,
  editable,
  labels,
  retryable,
  type Config,
  type Progress,
  type Task,
} from "./types";
import SetupWizard from "./SetupWizard";

type Page = "tasks" | "history" | "account" | "settings";
type Notice = { kind: "success" | "error"; text: string };
const NoticeContext = createContext<Notice | null>(null);
const titles = {
  tasks: "任务中心",
  history: "投稿记录",
  account: "账号与连接",
  settings: "设置",
};

function Modal({
  title,
  children,
  close,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const notice = useContext(NoticeContext);
  useEffect(() => {
    ref.current?.showModal();
    return () => ref.current?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={wide ? "modal wide" : "modal"}
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
    >
      <div className="modal-head">
        <h2>{title}</h2>
        <button className="icon-button" aria-label="关闭对话框" onClick={close}>
          <X size={19} />
        </button>
      </div>
      {notice?.kind === "error" && (
        <div className="inline-error" role="alert">
          {notice.text}
        </div>
      )}
      {children}
    </dialog>
  );
}

function Status({ status }: { status: string }) {
  return (
    <span className={`status status-${status}`}>
      <span />
      {labels[status] || status}
    </span>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>("tasks");
  const [config, setConfig] = useState<Config | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [progress, setProgress] = useState<Record<string, Progress>>({});
  const [connected, setConnected] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [newTask, setNewTask] = useState(false);
  const [setup, setSetup] = useState(false);
  const [selected, setSelected] = useState<Task | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [auth, setAuth] = useState<any>({
    configured: false,
    login: { status: "idle" },
  });
  const [diagnostics, setDiagnostics] = useState<any>(null);
  const [closing, setClosing] = useState(false);
  const [queueActive, setQueueActive] = useState<any[]>([]);
  const action = useCallback(
    async (work: () => Promise<unknown>, success?: string) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      setNotice(null);
      try {
        await work();
        if (success) setNotice({ kind: "success", text: success });
      } catch (error) {
        setNotice({
          kind: "error",
          text: String(error instanceof Error ? error.message : error),
        });
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [],
  );
  const loadConfig = useCallback(
    async () => setConfig(await request("settings.get")),
    [],
  );
  const loadTasks = useCallback(async () => {
    const result = await request("tasks.list", {
      search: query,
      status: filter,
      offset,
      limit: 50,
      history: page === "history",
    });
    setTasks(result.items);
    setTotal(result.total);
    setCounts(result.counts);
    setQueueActive(result.queue.active);
    setSelected((old) =>
      old
        ? {
            ...old,
            ...result.items.find((t: Task) => t.video_id === old.video_id),
          }
        : null,
    );
  }, [query, filter, offset, page]);
  const refreshAccount = useCallback(
    async () => setAuth(await request("auth.status")),
    [],
  );

  useEffect(() => {
    let alive = true;
    request("system.health")
      .then(async (result) => {
        if (result.protocol_version !== 1)
          throw new Error("桌面与后台协议版本不匹配。");
        if (alive) {
          setConnected(true);
          setConnectionError("");
        }
        await Promise.all([loadConfig(), refreshAccount()]);
        request("system.diagnostics")
          .then((value) => {
            if (alive) setDiagnostics(value);
          })
          .catch(() => {});
      })
      .catch((error) => {
        if (alive) setConnectionError(String(error));
      });
    let stop = () => {};
    let stopClose = () => {};
    subscribe((event) => {
      if (!alive) return;
      if (event.event === "task.status") {
        setTasks((old) =>
          old.map((task) =>
            task.video_id === event.payload.video_id
              ? { ...task, ...event.payload }
              : task,
          ),
        );
        setSelected((old) =>
          old?.video_id === event.payload.video_id
            ? { ...old, ...event.payload }
            : old,
        );
        setProgress((old) => {
          const next = { ...old };
          delete next[event.payload.video_id];
          return next;
        });
      }
      if (event.event === "task.progress")
        setProgress((old) => ({
          ...old,
          [event.payload.video_id]: event.payload,
        }));
      if (event.event === "auth.status") {
        setAuth((old: any) => ({
          ...old,
          login: { ...old.login, ...event.payload },
        }));
        if (event.payload.status === "success") {
          void refreshAccount();
          setNotice({
            kind: "success",
            text: "B 站登录成功，凭据已保存在本机。",
          });
        }
      }
      if (event.event === "task.repaired")
        setNotice({
          kind: "success",
          text: "替换素材已准备好，请在创作中心替换原稿件的视频。",
        });
      if (event.event === "disconnected") {
        setConnected(false);
        setConnectionError(
          "后台连接已断开。请重新启动应用；未完成任务将在启动后恢复。",
        );
      }
    }).then((fn) => {
      if (alive) stop = fn;
      else fn();
    });
    onClose(() => setClosing(true)).then((fn) => {
      if (alive) stopClose = fn;
      else fn();
    });
    return () => {
      alive = false;
      stop();
      stopClose();
    };
  }, [loadConfig, refreshAccount]);

  useEffect(() => {
    if (!connected) return;
    let disposed = false;
    let loading = false;
    const update = async () => {
      if (loading || disposed) return;
      loading = true;
      try {
        await loadTasks();
      } catch {
        /* connection lifecycle reports the failure */
      } finally {
        loading = false;
      }
    };
    void update();
    const interval = setInterval(update, 1500);
    return () => {
      disposed = true;
      clearInterval(interval);
    };
  }, [connected, loadTasks]);

  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme =
        config?.theme === "system" || !config
          ? media.matches
            ? "dark"
            : "light"
          : config.theme;
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [config?.theme]);
  useEffect(() => {
    if (!notice || notice.kind === "error") return;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        if (connected) setNewTask(true);
      }
    };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  }, [connected]);

  const navigate = (next: Page) => {
    setPage(next);
    setSelected(null);
    setOffset(0);
    setFilter("");
    setQuery("");
  };
  const theme = async () => {
    const next =
      document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    setConfig(await request("settings.update", { values: { theme: next } }));
  };
  const exportLogs = () =>
    action(async () => {
      const path = await saveLog();
      if (path) await request("logs.export", { path });
    }, "诊断日志已导出。");
  const nav = [
    { id: "tasks", icon: ListVideo },
    { id: "history", icon: History },
    { id: "account", icon: KeyRound },
    { id: "settings", icon: Settings2 },
  ] as const;

  return (
    <NoticeContext.Provider value={notice}>
      <div className="app-shell">
        <aside className="sidebar">
          <a
            className="brand"
            href="#"
            onClick={(e) => {
              e.preventDefault();
              navigate("tasks");
            }}
          >
            <img src="/brand.png" alt="" />
            <span>
              yt2bili<small>by StarDazz</small>
            </span>
          </a>
          <div className="nav-label">工作空间</div>
          <nav aria-label="主导航">
            {nav.map((item) => (
              <button
                key={item.id}
                className={`nav-item ${page === item.id ? "selected" : ""}`}
                onClick={() => navigate(item.id)}
              >
                <item.icon size={18} />
                {titles[item.id]}
                {item.id === "tasks" && !!counts.ready && (
                  <span className="nav-count">{counts.ready}</span>
                )}
              </button>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <div className="local-card">
              <ShieldCheck size={18} />
              <div>
                在你的设备上运行<small>素材与凭据保留在本机</small>
              </div>
            </div>
            <div className="sidebar-footer">
              <button
                className="text-button studio"
                onClick={() => external("https://stardazz-com.vercel.app/")}
              >
                StarDazz <ExternalLink size={12} />
              </button>
              <button
                className="icon-button"
                title="切换明暗主题"
                aria-label="切换明暗主题"
                onClick={() => action(theme)}
                disabled={busy || !config}
              >
                <Sun size={16} />
              </button>
            </div>
            <span className="version">DESKTOP · 0.2.0 ALPHA</span>
          </div>
        </aside>
        <div className="workspace">
          <header className="topbar">
            <div className="breadcrumb">
              工作空间 <span>/</span> <strong>{titles[page]}</strong>
            </div>
            <div className="connection">
              <i className={connected ? "online" : ""} />
              {preview
                ? "界面预览 · 示例数据"
                : connected
                  ? "本地服务已连接"
                  : "正在连接本地服务"}
            </div>
          </header>
          <main>
            <div className="page-heading">
              <div>
                <span className="eyebrow">
                  {page === "tasks"
                    ? "YOUR LOCAL WORKFLOW"
                    : page === "history"
                      ? "PUBLISHING HISTORY"
                      : page === "account"
                        ? "CONNECTED SERVICES"
                        : "MAKE IT YOURS"}
                </span>
                <h1>{titles[page]}</h1>
                <p>
                  {page === "tasks"
                    ? "从链接到投稿，每一步都清晰可见。"
                    : page === "history"
                      ? "所有已提交稿件，以及需要你核对的结果。"
                      : page === "account"
                        ? "连接你的账号，开始本地创作流程。"
                        : "按你的习惯设置工作目录与投稿默认值。"}
                </p>
              </div>
              {page === "tasks" && (
                <button
                  className="primary"
                  onClick={() => setNewTask(true)}
                  disabled={!connected}
                >
                  <Plus size={17} />
                  新建任务 <kbd>⌘ / Ctrl N</kbd>
                </button>
              )}
            </div>
            {connectionError && (
              <div className="banner error">
                <CircleAlert size={18} />
                <div>
                  <strong>未连接到后台</strong>
                  <p>{connectionError}</p>
                </div>
                <button className="secondary" onClick={() => location.reload()}>
                  重新连接
                </button>
              </div>
            )}
            {notice && (
              <div
                className={`notice ${notice.kind}`}
                role={notice.kind === "error" ? "alert" : "status"}
              >
                {notice.kind === "error" ? (
                  <CircleAlert size={18} />
                ) : (
                  <CheckCircle2 size={18} />
                )}
                <span>{notice.text}</span>
                <button
                  aria-label="关闭提示"
                  className="icon-button"
                  onClick={() => setNotice(null)}
                >
                  <X size={16} />
                </button>
              </div>
            )}
            {(page === "tasks" || page === "history") && (
              <>
                {page === "tasks" && (
                  <div className="stats-strip">
                    {[
                      {
                        label: "下载中",
                        value: counts.downloading,
                        icon: ArrowDownToLine,
                      },
                      {
                        label: "校验中",
                        value: counts.validating,
                        icon: ShieldCheck,
                      },
                      {
                        label: "投稿中",
                        value: counts.uploading,
                        icon: Upload,
                      },
                      { label: "待预览", value: counts.ready, icon: Video },
                    ].map((s, index) => (
                      <div className="stat" key={s.label}>
                        <span className="stat-icon">
                          <s.icon size={18} />
                        </span>
                        <div>
                          <span>{s.label}</span>
                          <strong>
                            {String(s.value || 0).padStart(2, "0")}
                          </strong>
                        </div>
                        {index < 3 && <small>单路队列</small>}
                      </div>
                    ))}
                  </div>
                )}
                {config && !config.has_deepl_key && page === "tasks" && (
                  <div className="setup-banner">
                    <div className="setup-icon">
                      <Zap size={20} />
                    </div>
                    <div>
                      <strong>先完成一次简单的配置</strong>
                      <p>
                        添加 DeepL 密钥并连接 B
                        站账号，之后就可以在这里管理任务。
                      </p>
                    </div>
                    <button
                      className="secondary"
                      onClick={() => setSetup(true)}
                    >
                      开始配置 <ArrowRight size={15} />
                    </button>
                  </div>
                )}
                <div className="task-panel">
                  <div className="panel-toolbar">
                    <div className="tabs">
                      <button
                        className={!filter ? "current" : ""}
                        onClick={() => {
                          setFilter("");
                          setOffset(0);
                        }}
                      >
                        全部任务 <span>{total}</span>
                      </button>
                      {(page === "history"
                        ? ["submitted", "submission_unknown"]
                        : ["ready", "failed"]
                      ).map((status) => (
                        <button
                          key={status}
                          className={filter === status ? "current" : ""}
                          onClick={() => {
                            setFilter(status);
                            setOffset(0);
                          }}
                        >
                          {labels[status]}
                        </button>
                      ))}
                    </div>
                    <label className="search">
                      <Search size={16} />
                      <input
                        aria-label="搜索任务"
                        placeholder="搜索标题或视频 ID"
                        value={query}
                        onChange={(e) => {
                          setQuery(e.target.value);
                          setOffset(0);
                        }}
                      />
                    </label>
                  </div>
                  <div className="table-head">
                    <span>视频 / 标题</span>
                    <span>当前状态</span>
                    <span>最近更新</span>
                    <span />
                  </div>
                  {tasks.length ? (
                    <div className="task-rows">
                      {tasks.map((task) => (
                        <button
                          key={task.video_id}
                          className="task-row"
                          onClick={() =>
                            action(async () =>
                              setSelected(
                                await request("tasks.get", {
                                  video_id: task.video_id,
                                }),
                              ),
                            )
                          }
                        >
                          <span className="task-title">
                            <span className="video-tile">
                              <Video size={20} />
                            </span>
                            <span>
                              <strong>
                                {task.title_zh ||
                                  task.title_orig ||
                                  "等待读取视频信息"}
                              </strong>
                              <small>
                                {task.video_id}
                                {task.uploader && ` · ${task.uploader}`}
                              </small>
                            </span>
                          </span>
                          <span>
                            <Status status={task.status} />
                            {active(task) && progress[task.video_id] && (
                              <span className="mini-progress">
                                <i
                                  style={{
                                    width: `${progress[task.video_id].percent ?? 25}%`,
                                  }}
                                />
                              </span>
                            )}
                          </span>
                          <time>
                            {new Date(
                              task.updated_at || task.created_at,
                            ).toLocaleString("zh-CN", {
                              month: "2-digit",
                              day: "2-digit",
                              hour: "2-digit",
                              minute: "2-digit",
                            })}
                          </time>
                          <ChevronRight size={17} />
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="empty-state">
                      <div className="empty-art">
                        <span />
                        <span />
                        <div>
                          <Link2 size={30} strokeWidth={1.4} />
                        </div>
                      </div>
                      <h2>
                        {query || filter
                          ? "没有找到匹配的任务"
                          : page === "history"
                            ? "还没有投稿记录"
                            : "你的下一条视频，从这里开始"}
                      </h2>
                      <p>
                        {query || filter
                          ? "试试其他关键词，或查看全部任务。"
                          : page === "history"
                            ? "完成投稿后，BV 号和提交记录会保留在这里。"
                            : "粘贴 YouTube 链接，我们会为你下载、校验并翻译素材。"}
                      </p>
                      {!query && !filter && page === "tasks" && (
                        <button
                          className="secondary"
                          onClick={() => setNewTask(true)}
                          disabled={!connected}
                        >
                          <Plus size={16} />
                          添加第一个任务
                        </button>
                      )}
                      <div className="workflow-hint">
                        <span>01 下载</span>
                        <ArrowRight size={12} />
                        <span>02 校验</span>
                        <ArrowRight size={12} />
                        <span>03 预览与投稿</span>
                      </div>
                    </div>
                  )}
                  <div className="panel-footer">
                    <span>
                      {page === "tasks"
                        ? "默认先准备素材，由你确认后投稿"
                        : "已提交不代表通过平台审核"}
                    </span>
                    <div>
                      <span>{total} 条记录</span>
                      <button
                        aria-label="上一页"
                        className="icon-button"
                        disabled={!offset}
                        onClick={() => setOffset((v) => Math.max(0, v - 50))}
                      >
                        <ChevronLeft size={15} />
                      </button>
                      <button
                        aria-label="下一页"
                        className="icon-button"
                        disabled={offset + 50 >= total}
                        onClick={() => setOffset((v) => v + 50)}
                      >
                        <ChevronRight size={15} />
                      </button>
                    </div>
                  </div>
                </div>
                <div className="page-footnote">
                  <ShieldCheck size={14} />
                  仅处理你拥有版权或已获授权的视频。下载、校验、上传各自排队，互不阻塞。
                </div>
              </>
            )}
            {page === "account" && config && (
              <Account
                config={config}
                auth={auth}
                busy={busy}
                action={action}
                refresh={async () => {
                  await loadConfig();
                  await refreshAccount();
                }}
                setAuth={setAuth}
              />
            )}
            {page === "settings" && config && (
              <Settings
                config={config}
                busy={busy}
                action={action}
                refresh={loadConfig}
                diagnostics={diagnostics}
                setDiagnostics={setDiagnostics}
                exportLogs={exportLogs}
              />
            )}
          </main>
          <footer className="bottom-bar">
            <span>
              <Activity size={12} />
              {queueActive.length
                ? `${queueActive.length} 个任务正在队列中`
                : "所有队列空闲"}
            </span>
            <span>本地处理 · 由你掌控</span>
          </footer>
        </div>
        {setup && config && (
          <Modal
            title="欢迎使用 yt2bili"
            close={() => {
              if (!busy)
                action(async () => {
                  await request("auth.login.cancel");
                  setSetup(false);
                });
            }}
          >
            <SetupWizard
              config={config}
              auth={auth}
              diagnostics={diagnostics}
              busy={busy}
              action={action}
              refresh={async () => {
                await loadConfig();
                await refreshAccount();
              }}
              setDiagnostics={setDiagnostics}
              done={() => setSetup(false)}
            />
          </Modal>
        )}
        {newTask && (
          <NewTask
            busy={busy}
            action={action}
            close={() => {
              if (!busy) setNewTask(false);
            }}
            done={async () => {
              setNewTask(false);
              await loadTasks();
            }}
          />
        )}
        {selected && (
          <TaskDetail
            task={selected}
            progress={progress[selected.video_id]}
            busy={busy}
            action={action}
            close={() => setSelected(null)}
            refresh={async () => {
              await loadTasks();
              setSelected(
                await request("tasks.get", { video_id: selected.video_id }),
              );
            }}
          />
        )}
        {closing && (
          <Modal title="退出 yt2bili" close={() => setClosing(false)}>
            <p className="modal-copy">
              {queueActive.length
                ? "还有任务在运行。投稿中的任务需要完成后再退出；其他任务可取消并保留素材。"
                : "当前没有运行中的任务，可以安全退出。"}
            </p>
            <div className="modal-actions">
              <button className="secondary" onClick={() => setClosing(false)}>
                继续使用
              </button>
              {queueActive.length ? (
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() =>
                    action(async () => {
                      for (const item of queueActive) {
                        const task = await request<Task>("tasks.get", {
                          video_id: item.video_id,
                        });
                        if (task.status !== "uploading")
                          await request("tasks.cancel", {
                            video_id: item.video_id,
                          });
                      }
                    }, "已请求取消，请等待队列停止后退出。")
                  }
                >
                  取消未提交任务
                </button>
              ) : (
                <button className="primary" onClick={() => closeApp()}>
                  退出应用
                </button>
              )}
            </div>
          </Modal>
        )}
      </div>
    </NoticeContext.Provider>
  );
}

type Action = (work: () => Promise<unknown>, success?: string) => Promise<void>;
function NewTask({
  busy,
  action,
  close,
  done,
}: {
  busy: boolean;
  action: Action;
  close: () => void;
  done: () => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [mode, setMode] = useState("preview");
  const [authorized, setAuthorized] = useState(false);
  const op = useRef(operationId());
  return (
    <Modal title="新建任务" close={close}>
      <p className="modal-copy">
        添加你有权处理的视频。每行一个链接，也可以导入 TXT 列表。
      </p>
      <label className="field">
        YouTube 视频链接
        <textarea
          autoFocus
          rows={6}
          placeholder={"https://www.youtube.com/watch?v=…\nhttps://youtu.be/…"}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            op.current = operationId();
          }}
        />
      </label>
      <button
        className="text-button"
        onClick={() =>
          action(async () => {
            const path = await chooseFile();
            if (path) {
              setText((await request("files.read_urls", { path })).text);
              op.current = operationId();
            }
          })
        }
        disabled={busy}
      >
        <FileText size={15} />从 TXT 文件导入
      </button>
      <div className="mode-options">
        <label className={mode === "preview" ? "chosen" : ""}>
          <input
            type="radio"
            name="mode"
            checked={mode === "preview"}
            onChange={() => {
              setMode("preview");
              op.current = operationId();
            }}
          />
          <span>
            <strong>
              准备素材并预览 <em>推荐</em>
            </strong>
            <small>下载、校验和翻译完成后，由你确认投稿。</small>
          </span>
        </label>
        <label className={mode === "auto" ? "chosen" : ""}>
          <input
            type="radio"
            name="mode"
            checked={mode === "auto"}
            onChange={() => {
              setMode("auto");
              op.current = operationId();
            }}
          />
          <span>
            <strong>自动投稿</strong>
            <small>素材准备好后，使用默认参数自动提交到 B 站。</small>
          </span>
        </label>
      </div>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={authorized}
          onChange={(e) => setAuthorized(e.target.checked)}
        />
        我拥有这些视频的版权或已获得转载授权。
      </label>
      <div className="modal-actions">
        <button className="secondary" onClick={close} disabled={busy}>
          取消
        </button>
        <button
          className="primary"
          disabled={busy || !text.trim() || !authorized}
          onClick={() =>
            action(async () => {
              const result = await request("tasks.create", {
                text,
                mode,
                operation_id: op.current,
              });
              if (!result.added.length)
                throw new Error("这些视频已有任务，请在列表中查看或继续。");
              await done();
            }, "任务已加入队列。")
          }
        >
          {busy ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}
          加入队列
        </button>
      </div>
    </Modal>
  );
}

function TaskDetail({
  task,
  progress,
  busy,
  action,
  close,
  refresh,
}: {
  task: Task;
  progress?: Progress;
  busy: boolean;
  action: Action;
  close: () => void;
  refresh: () => Promise<void>;
}) {
  const [tab, setTab] = useState("metadata");
  const [title, setTitle] = useState(task.title_zh);
  const [description, setDescription] = useState(task.desc_zh);
  const [cover, setCover] = useState<string | null>(null);
  const [logs, setLogs] = useState<any[]>([]);
  const [confirm, setConfirm] = useState("");
  const [bv, setBv] = useState("");
  const op = useRef(operationId());
  useEffect(() => {
    setTitle(task.title_zh);
    setDescription(task.desc_zh);
  }, [task.video_id, task.title_zh, task.desc_zh]);
  useEffect(() => {
    let alive = true;
    request("tasks.cover", { video_id: task.video_id })
      .then((value) => {
        if (alive) setCover(value.image);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [task.video_id, task.cover_path]);
  useEffect(() => {
    if (tab !== "logs") return;
    const load = () =>
      request("logs.tail", { video_id: task.video_id })
        .then((value) => setLogs(value.items))
        .catch(() => {});
    void load();
    const timer = setInterval(load, 1500);
    return () => clearInterval(timer);
  }, [task.video_id, tab]);
  const command = (method: string, success: string) =>
    action(async () => {
      await request(method, {
        video_id: task.video_id,
        operation_id: op.current,
      });
      op.current = operationId();
      setConfirm("");
      await refresh();
    }, success);
  return (
    <Modal title="任务详情" close={close} wide>
      <div className="detail-summary">
        {cover ? (
          <img className="cover" src={cover} alt="视频封面" />
        ) : (
          <div className="cover placeholder">
            <Video size={35} />
          </div>
        )}
        <div>
          <Status status={task.status} />
          <h3>{task.title_zh || task.title_orig || task.video_id}</h3>
          <p>
            {task.uploader || "等待读取作者"} · {task.video_id}
          </p>
          <button
            className="text-button"
            onClick={() =>
              action(async () => {
                await request("tasks.open_folder", { video_id: task.video_id });
              })
            }
            disabled={!task.work_dir}
          >
            <FolderOpen size={14} />
            打开素材目录
          </button>
          {task.bv_id && (
            <button
              className="text-button"
              onClick={() =>
                external(`https://www.bilibili.com/video/${task.bv_id}`)
              }
            >
              <ExternalLink size={14} />
              {task.bv_id}
            </button>
          )}
        </div>
      </div>
      {task.error && <div className="inline-error">{task.error}</div>}
      {active(task) && (
        <div className="detail-progress">
          <span>
            {labels[progress?.stage || task.status]}
            {progress?.track &&
              ` · ${progress.track} · ${progress.backend}`}{" "}
            {progress?.remaining
              ? `· 剩余 ${Math.ceil(progress.remaining)} 秒`
              : ""}
          </span>
          <strong>
            {progress?.percent != null
              ? `${progress.percent.toFixed(1)}%`
              : "进行中"}
          </strong>
          <div>
            <i
              className={progress?.percent == null ? "indeterminate" : ""}
              style={{ width: `${progress?.percent ?? 30}%` }}
            />
          </div>
        </div>
      )}
      <div className="tabs detail-tabs">
        <button
          className={tab === "metadata" ? "current" : ""}
          onClick={() => setTab("metadata")}
        >
          投稿素材
        </button>
        <button
          className={tab === "original" ? "current" : ""}
          onClick={() => setTab("original")}
        >
          原始信息
        </button>
        <button
          className={tab === "logs" ? "current" : ""}
          onClick={() => setTab("logs")}
        >
          运行日志
        </button>
      </div>
      {tab === "metadata" && (
        <div className="detail-form">
          <label className="field">
            中文标题 <span>{title.length} / 80</span>
            <input
              value={title}
              maxLength={80}
              readOnly={!editable(task)}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label className="field">
            简介 <span>{description.length} / 2000</span>
            <textarea
              rows={7}
              value={description}
              maxLength={2000}
              readOnly={!editable(task)}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <p className="help">
            简介中的原标题、作者与来源链接会保留。获得 BV
            号后，任务素材将自动清理。
          </p>
          {editable(task) && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() =>
                action(async () => {
                  await request("tasks.update_metadata", {
                    video_id: task.video_id,
                    title,
                    description,
                  });
                  await refresh();
                }, "投稿信息已保存。")
              }
            >
              <Check size={15} />
              保存修改
            </button>
          )}
        </div>
      )}
      {tab === "original" && (
        <div className="original-info">
          <h3>{task.title_orig || "尚未读取"}</h3>
          <p>{task.url}</p>
          <pre>{task.desc_orig || "暂无原始简介"}</pre>
        </div>
      )}
      {tab === "logs" && (
        <div className="log-view">
          {logs.length ? (
            logs.map((line, i) => (
              <div key={i}>
                <time>{line.time}</time>
                <span className={line.level === "ERROR" ? "error-text" : ""}>
                  {line.message}
                </span>
              </div>
            ))
          ) : (
            <p>本次会话暂无该任务日志。</p>
          )}
        </div>
      )}
      {task.status === "submission_unknown" && (
        <div className="resolve-box">
          <strong>先核对创作中心，再继续处理</strong>
          <p>网络中断不一定代表投稿失败。登记 BV 号会保留本地素材。</p>
          <button
            className="text-button"
            onClick={() =>
              external(
                "https://member.bilibili.com/platform/upload-manager/article",
              )
            }
          >
            打开创作中心 <ExternalLink size={14} />
          </button>
          <div className="inline-controls">
            <input
              aria-label="登记 BV 号"
              value={bv}
              onChange={(e) => setBv(e.target.value)}
              placeholder="BV…"
            />
            <button
              className="secondary"
              disabled={busy}
              onClick={() =>
                action(async () => {
                  await request("tasks.resolve", {
                    video_id: task.video_id,
                    bv_id: bv,
                  });
                  await refresh();
                }, "BV 号已登记。")
              }
            >
              登记已提交稿件
            </button>
          </div>
          <button className="text-button" onClick={() => setConfirm("resolve")}>
            我已核对，确实没有提交
          </button>
        </div>
      )}
      {confirm && (
        <div className="confirm-box">
          <strong>
            {confirm === "submit"
              ? "确认将当前素材投稿到 B 站？"
              : confirm === "repair"
                ? "准备原稿件的替换视频？此操作不会投稿。"
                : "确认创作中心没有这条稿件？"}
          </strong>
          <div>
            <button className="secondary" onClick={() => setConfirm("")}>
              返回
            </button>
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                confirm === "submit"
                  ? command("tasks.submit", "已进入投稿队列。")
                  : confirm === "repair"
                    ? command("tasks.repair", "已开始准备替换素材。")
                    : action(async () => {
                        await request("tasks.resolve", {
                          video_id: task.video_id,
                          not_submitted: true,
                        });
                        setConfirm("");
                        await refresh();
                      }, "已标记为可继续处理。")
              }
            >
              确认
            </button>
          </div>
        </div>
      )}
      <div className="modal-actions">
        <span className="help">已提交 ≠ 已过审</span>
        <button className="secondary" onClick={close}>
          关闭
        </button>
        {retryable(task) && (
          <button
            className="primary"
            disabled={busy}
            onClick={() =>
              command("tasks.retry", "已继续准备素材，完成后等待预览。")
            }
          >
            <RefreshCw size={15} />
            继续任务
          </button>
        )}
        {editable(task) && (
          <button
            className="primary"
            disabled={
              busy || title !== task.title_zh || description !== task.desc_zh
            }
            title="请先保存修改"
            onClick={() => setConfirm("submit")}
          >
            <Send size={15} />
            确认投稿
          </button>
        )}
        {task.status === "submitted" && task.bv_id && (
          <button
            className="secondary"
            disabled={busy}
            onClick={() => setConfirm("repair")}
          >
            准备修复素材
          </button>
        )}
        {active(task) && task.status !== "uploading" && (
          <button
            className="secondary danger"
            disabled={busy || task.status === "cancel_requested"}
            onClick={() =>
              action(async () => {
                await request("tasks.cancel", { video_id: task.video_id });
                await refresh();
              }, "已请求取消，素材会保留。")
            }
          >
            取消任务
          </button>
        )}
      </div>
    </Modal>
  );
}

function Account({
  config,
  auth,
  busy,
  action,
  refresh,
  setAuth,
}: {
  config: Config;
  auth: any;
  busy: boolean;
  action: Action;
  refresh: () => Promise<void>;
  setAuth: (v: any) => void;
}) {
  const [key, setKey] = useState("");
  const [showLogin, setShowLogin] = useState(false);
  const [browser, setBrowser] = useState("edge");
  const login = auth.login || {};
  const importCookie = (kind: string) =>
    action(async () => {
      const path = await chooseFile(kind === "bilibili" ? ["json"] : ["txt"]);
      if (path) {
        await request("auth.import", { path, kind });
        await refresh();
      }
    }, "Cookie 已导入本机。");
  const loginLabels: Record<string, string> = {
    loading: "正在获取二维码…",
    waiting: "请使用哔哩哔哩 App 扫码并确认",
    scanned: "已扫码，请在手机上确认",
    expired: "二维码已过期，请刷新",
    success: "登录成功",
    failed: "获取二维码失败",
    idle: "准备扫码登录",
  };
  return (
    <div className="settings-stack">
      <section className="settings-card">
        <div className="section-title">
          <div className="service-icon">
            <Video size={23} />
          </div>
          <div>
            <h2>哔哩哔哩</h2>
            <p>连接投稿账号，登录凭据仅保存在本机。</p>
          </div>
          <span className={`pill ${auth.configured ? "positive" : ""}`}>
            {auth.verified
              ? "登录有效"
              : auth.configured
                ? "已配置 · 待检测"
                : "未连接"}
          </span>
        </div>
        <div className="section-body">
          <p className="help">
            {auth.name
              ? `当前账号：${auth.name}`
              : auth.mid
                ? `账号 UID：${auth.mid}`
                : "使用 B 站 App 扫码连接，或导入已有 biliup 登录文件。"}
          </p>
          <div className="button-row">
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                action(async () => {
                  await request("auth.login.start");
                  setShowLogin(true);
                }, undefined)
              }
            >
              扫码登录 <ArrowRight size={15} />
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => importCookie("bilibili")}
            >
              导入登录文件
            </button>
            <button
              className="text-button"
              disabled={busy || !auth.configured}
              onClick={() =>
                action(async () => {
                  setAuth(await request("auth.renew"));
                }, "登录态检测完成。")
              }
            >
              <RefreshCw size={14} />
              刷新与检测
            </button>
          </div>
        </div>
      </section>
      <section className="settings-card">
        <div className="section-title">
          <div className="service-icon">
            <span className="deepl-mark">D</span>
          </div>
          <div>
            <h2>DeepL 翻译</h2>
            <p>将视频标题与简介翻译为中文。</p>
          </div>
          <span className={`pill ${config.has_deepl_key ? "positive" : ""}`}>
            {config.has_deepl_key ? "已配置" : "未配置"}
          </span>
        </div>
        <div className="section-body">
          <label className="field">
            API 密钥
            <div className="inline-controls">
              <input
                type="password"
                autoComplete="off"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                placeholder={
                  config.has_deepl_key
                    ? "已安全保存，输入新密钥可替换"
                    : "输入你的 DeepL API Free 密钥"
                }
              />
              <button
                className="primary"
                disabled={busy || !key.trim()}
                onClick={() =>
                  action(async () => {
                    await request("credentials.set", { value: key });
                    setKey("");
                    await refresh();
                  }, "密钥已保存到系统凭据存储。")
                }
              >
                保存密钥
              </button>
            </div>
          </label>
          <div className="button-row">
            <span className="help">
              <ShieldCheck size={13} />
              保存在系统凭据存储，不写入普通配置文件。
            </span>
            <button
              className="text-button"
              disabled={busy || !config.has_deepl_key}
              onClick={() =>
                action(async () => {
                  const result = await request("credentials.test");
                  if (!result.ok) throw new Error("连接失败");
                }, "DeepL 连接正常。")
              }
            >
              测试连接
            </button>
          </div>
          {config.vault_error && (
            <div className="inline-error">{config.vault_error}</div>
          )}
        </div>
      </section>
      <section className="settings-card">
        <div className="section-title">
          <div className="service-icon">
            <Link2 size={23} />
          </div>
          <div>
            <h2>YouTube 访问</h2>
            <p>当源站要求登录验证时，提供你的浏览器 Cookie。</p>
          </div>
          <span className="pill">
            {config.youtube_cookies ? "已导入" : "可选配置"}
          </span>
        </div>
        <div className="section-body">
          <div className="button-row">
            <button
              className="secondary"
              disabled={busy}
              onClick={() => importCookie("youtube")}
            >
              <Upload size={15} />
              导入 Cookie 文件
            </button>
            <select
              aria-label="浏览器"
              value={browser}
              onChange={(e) => setBrowser(e.target.value)}
            >
              <option value="edge">Edge</option>
              <option value="chrome">Chrome</option>
              <option value="firefox">Firefox</option>
            </select>
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                action(async () => {
                  await request("auth.youtube_export", { browser });
                  await refresh();
                }, "浏览器 Cookie 已导出到本机。")
              }
            >
              从浏览器导出
            </button>
          </div>
          <p className="help">
            导出前请完全退出对应浏览器。若系统加密或权限阻止读取，可改用
            Netscape 格式 TXT 文件导入。
          </p>
        </div>
      </section>
      {showLogin && (
        <Modal
          title="连接哔哩哔哩"
          close={() => {
            setShowLogin(false);
            void request("auth.login.cancel");
          }}
        >
          <div className="qr-panel">
            {["waiting", "scanned"].includes(login.status) && login.qrcode ? (
              <img src={login.qrcode} alt="B 站登录二维码" />
            ) : login.status === "success" ? (
              <CheckCircle2 size={64} />
            ) : (
              <div className="qr-placeholder">
                {login.status === "loading" ? (
                  <Loader2 className="spin" size={36} />
                ) : (
                  <RefreshCw size={36} />
                )}
              </div>
            )}
            <h3>{loginLabels[login.status] || "准备登录"}</h3>
            {login.message && <p className="error-text">{login.message}</p>}
            <p className="help">二维码只用于本次登录，请勿分享给他人。</p>
          </div>
          <div className="modal-actions">
            <button
              className="secondary"
              onClick={() => {
                setShowLogin(false);
                void request("auth.login.cancel");
              }}
            >
              关闭
            </button>
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                action(async () => {
                  await request("auth.login.start");
                })
              }
            >
              刷新二维码
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function Settings({
  config,
  busy,
  action,
  refresh,
  diagnostics,
  setDiagnostics,
  exportLogs,
}: {
  config: Config;
  busy: boolean;
  action: Action;
  refresh: () => Promise<void>;
  diagnostics: any;
  setDiagnostics: (v: any) => void;
  exportLogs: () => void;
}) {
  const [form, setForm] = useState(config);
  const [importConfirm, setImportConfirm] = useState(false);
  useEffect(() => setForm(config), [config]);
  const update = (key: keyof Config, value: unknown) =>
    setForm((old) => ({ ...old, [key]: value }));
  return (
    <div className="settings-stack">
      <section className="settings-card">
        <div className="section-title">
          <div>
            <h2>工作目录</h2>
            <p>视频素材可能占用较多空间，建议选择独立的工作文件夹。</p>
          </div>
        </div>
        <div className="section-body">
          <label className="field">
            素材保存位置
            <div className="inline-controls">
              <input
                value={form.work_dir}
                onChange={(e) => update("work_dir", e.target.value)}
              />
              <button
                className="secondary"
                disabled={busy}
                onClick={() =>
                  action(async () => {
                    const path = await chooseDirectory();
                    if (path) update("work_dir", path);
                  })
                }
              >
                <FolderOpen size={16} />
                选择目录
              </button>
            </div>
          </label>
          <p className="help">
            更改后只影响新任务；已有素材保持原位。
            {diagnostics && `当前可用空间 ${bytes(diagnostics.free_bytes)}。`}
          </p>
          <p className="data-path">任务与设置：{config.data_dir}</p>
        </div>
      </section>
      <section className="settings-card">
        <div className="section-title">
          <div>
            <h2>投稿默认值</h2>
            <p>新建任务时保存参数快照，修改默认值不影响已有任务。</p>
          </div>
        </div>
        <div className="section-body form-grid">
          <label className="field">
            投稿分区 ID
            <input
              type="number"
              min={1}
              max={65535}
              value={form.bili_tid}
              onChange={(e) => update("bili_tid", Number(e.target.value))}
            />
          </label>
          <label className="field">
            上传线路
            <select
              value={form.bili_line}
              onChange={(e) => update("bili_line", e.target.value)}
            >
              {["tx", "bda2", "qn", "ws", "txa"].map((line) => (
                <option key={line}>{line}</option>
              ))}
            </select>
          </label>
          <label className="field">
            标签
            <input
              value={form.bili_tags}
              onChange={(e) => update("bili_tags", e.target.value)}
              placeholder="转载,科技"
            />
          </label>
          <label className="field">
            上传间隔（秒）
            <input
              type="number"
              min={0}
              max={600}
              value={form.upload_gap_seconds}
              onChange={(e) =>
                update("upload_gap_seconds", Number(e.target.value))
              }
            />
          </label>
        </div>
      </section>
      <section className="settings-card">
        <div className="section-title">
          <div>
            <h2>外观与校验</h2>
            <p>沿用 StarDazz 的简洁界面，也保留可靠的素材检查。</p>
          </div>
        </div>
        <div className="section-body">
          <div className="setting-row">
            <div>
              <strong>界面主题</strong>
              <p>选择浅色、深色或跟随系统。</p>
            </div>
            <div className="segmented">
              {[
                { id: "light", icon: Sun, name: "浅色" },
                { id: "dark", icon: Moon, name: "深色" },
                { id: "system", icon: Monitor, name: "系统" },
              ].map((theme) => (
                <button
                  key={theme.id}
                  className={form.theme === theme.id ? "chosen" : ""}
                  onClick={() => update("theme", theme.id)}
                >
                  <theme.icon size={14} />
                  {theme.name}
                </button>
              ))}
            </div>
          </div>
          <div className="setting-row">
            <div>
              <strong>完整校验</strong>
              <p>自动尝试硬件解码，不可用时回退 CPU。</p>
            </div>
            <select
              value={form.hwaccel}
              onChange={(e) => update("hwaccel", e.target.value)}
            >
              <option value="auto">自动选择</option>
              <option value="cpu">仅 CPU</option>
            </select>
          </div>
          <label className="setting-row">
            <div>
              <strong>复用完整校验缓存</strong>
              <p>仍检查整文件 SHA-256，命中后跳过重复解码。</p>
            </div>
            <input
              type="checkbox"
              checked={form.validation_cache}
              onChange={(e) => update("validation_cache", e.target.checked)}
            />
          </label>
        </div>
      </section>
      <div className="save-row">
        <span className="help">任务运行期间不能修改全局设置。</span>
        <button
          className="primary"
          disabled={busy}
          onClick={() =>
            action(async () => {
              const {
                work_dir,
                bili_tid,
                bili_tags,
                bili_line,
                upload_gap_seconds,
                theme,
                hwaccel,
                validation_cache,
              } = form;
              await request("settings.update", {
                values: {
                  work_dir,
                  bili_tid,
                  bili_tags,
                  bili_line,
                  upload_gap_seconds,
                  theme,
                  hwaccel,
                  validation_cache,
                },
              });
              await refresh();
            }, "设置已保存。")
          }
        >
          <Check size={16} />
          保存设置
        </button>
      </div>
      <section className="settings-card">
        <div className="section-title">
          <div>
            <h2>环境与数据</h2>
            <p>检查本地工具，或导入命令行版本的任务记录。</p>
          </div>
          <button
            className="text-button"
            disabled={busy}
            onClick={() =>
              action(async () =>
                setDiagnostics(await request("system.diagnostics")),
              )
            }
          >
            <RefreshCw size={14} />
            重新检测
          </button>
        </div>
        <div className="section-body">
          <div className="tool-list">
            {diagnostics?.tools.map((tool: any) => (
              <div className="tool-row" key={tool.name}>
                <span>
                  {tool.available ? (
                    <CheckCircle2 size={16} />
                  ) : (
                    <CircleAlert size={16} />
                  )}
                  {tool.name}
                </span>
                <small title={tool.path}>{tool.version}</small>
              </div>
            ))}
          </div>
          <p className="help">
            YouTube 解析需要 Deno 或 Node.js
            中至少一个。第一期开发版使用本机工具，后续安装包将随附依赖。
          </p>
          <div className="button-row">
            <button
              className="secondary"
              disabled={busy}
              onClick={() => setImportConfirm(true)}
            >
              导入旧项目任务
            </button>
            <button className="secondary" disabled={busy} onClick={exportLogs}>
              <ArrowDownToLine size={15} />
              导出诊断日志
            </button>
          </div>
        </div>
      </section>
      {importConfirm && (
        <Modal title="导入旧项目记录" close={() => setImportConfirm(false)}>
          <p className="modal-copy">
            请先停止旧命令行程序。将复制任务记录并保留原素材位置，不会移动视频或导入密钥。已存在的视频
            ID 会跳过。
          </p>
          <div className="modal-actions">
            <button
              className="secondary"
              onClick={() => setImportConfirm(false)}
            >
              取消
            </button>
            <button
              className="primary"
              disabled={busy}
              onClick={() =>
                action(async () => {
                  const path = await chooseDirectory();
                  if (path) {
                    await request("data.import", { path });
                    setImportConfirm(false);
                  }
                }, "旧任务记录已导入。")
              }
            >
              选择旧项目目录
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
