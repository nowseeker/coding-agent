"use strict";

const state = {
  projects: [],
  selectedProjectId: null,
  selectedConversationId: null,
  currentConversation: null,
  activeJobId: null,
  pollTimer: null,
};

const elements = {
  projectList: document.querySelector("#project-list"),
  conversationList: document.querySelector("#conversation-list"),
  createProjectButton: document.querySelector("#create-project-button"),
  newConversationButton: document.querySelector("#new-conversation-button"),
  projectDialog: document.querySelector("#project-dialog"),
  projectForm: document.querySelector("#project-form"),
  projectName: document.querySelector("#project-name"),
  chatTitle: document.querySelector("#chat-title"),
  workspacePath: document.querySelector("#workspace-path"),
  messageList: document.querySelector("#message-list"),
  composer: document.querySelector("#composer"),
  messageInput: document.querySelector("#message-input"),
  sendButton: document.querySelector("#send-button"),
  traceList: document.querySelector("#trace-list"),
  runState: document.querySelector("#run-state"),
  runtimeDot: document.querySelector("#runtime-dot"),
  runtimeTitle: document.querySelector("#runtime-title"),
  runtimeDetail: document.querySelector("#runtime-detail"),
  toast: document.querySelector("#toast"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `请求失败：HTTP ${response.status}`);
  }
  return data;
}

async function bootstrap() {
  try {
    const data = await api("/api/bootstrap");
    state.projects = data.projects;
    renderRuntime(data.runtime);
    renderProjects();
    if (state.projects.length) {
      await selectProject(state.projects[0].id);
    }
  } catch (error) {
    showError(error);
  }
}

function renderRuntime(runtime) {
  elements.runtimeDot.classList.toggle("ready", runtime.api_ready);
  elements.runtimeTitle.textContent = runtime.api_ready ? "模型配置就绪" : "模型配置不完整";
  if (runtime.api_ready) {
    elements.runtimeDetail.textContent = `${runtime.model} · ${runtime.key_source} · 最大 ${runtime.max_iterations} 轮`;
  } else if (runtime.misnamed_variables?.length) {
    elements.runtimeDetail.textContent = `变量名不能包含反斜杠：${runtime.misnamed_variables.join(", ")}`;
  } else if (!runtime.key_configured && !runtime.model) {
    elements.runtimeDetail.textContent = "UI 进程未读取到 API Key 和模型；配置后需重启服务";
  } else if (!runtime.key_configured) {
    elements.runtimeDetail.textContent = "UI 进程未读取到 API Key；请在同一终端配置后重启";
  } else {
    elements.runtimeDetail.textContent = "已读取 API Key，但未读取模型名；配置后需重启服务";
  }
}

function renderProjects() {
  elements.projectList.replaceChildren();
  if (!state.projects.length) {
    elements.projectList.append(createPlaceholder("还没有项目"));
    return;
  }
  for (const project of state.projects) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `nav-item${project.id === state.selectedProjectId ? " active" : ""}`;
    button.textContent = project.name;
    const detail = document.createElement("small");
    detail.textContent = `${project.conversations.length} 个对话`;
    button.append(detail);
    button.addEventListener("click", () => selectProject(project.id));
    elements.projectList.append(button);
  }
}

async function selectProject(projectId) {
  state.selectedProjectId = projectId;
  const project = state.projects.find((item) => item.id === projectId);
  const latestConversation = project?.conversations?.length
    ? [...project.conversations].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0]
    : null;
  state.selectedConversationId = latestConversation?.id || null;
  renderProjects();
  renderConversations();
  elements.newConversationButton.disabled = false;
  if (state.selectedConversationId) {
    await selectConversation(state.selectedConversationId);
  } else {
    clearConversation(project);
  }
}

function renderConversations() {
  elements.conversationList.replaceChildren();
  const project = selectedProject();
  if (!project || !project.conversations.length) {
    elements.conversationList.append(createPlaceholder("还没有对话"));
    return;
  }
  const conversations = [...project.conversations].sort((a, b) =>
    b.updated_at.localeCompare(a.updated_at),
  );
  for (const conversation of conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `nav-item${conversation.id === state.selectedConversationId ? " active" : ""}`;
    button.textContent = conversation.title;
    const detail = document.createElement("small");
    detail.textContent = `${conversation.message_count} 条消息`;
    button.append(detail);
    button.addEventListener("click", () => selectConversation(conversation.id));
    elements.conversationList.append(button);
  }
}

async function selectConversation(conversationId) {
  if (state.activeJobId && conversationId !== state.selectedConversationId) {
    showError(new Error("当前任务运行期间请留在本对话。"));
    return;
  }
  try {
    const data = await api(`/api/conversations/${conversationId}`);
    state.selectedProjectId = data.project.id;
    state.selectedConversationId = conversationId;
    state.currentConversation = data.conversation;
    elements.chatTitle.textContent = data.conversation.title;
    elements.workspacePath.textContent = data.project.path;
    elements.messageInput.disabled = Boolean(state.activeJobId);
    elements.sendButton.disabled = Boolean(state.activeJobId);
    renderProjects();
    renderConversations();
    renderMessages();
    renderSavedTrace();
    if (!state.activeJobId) {
      const lastMessage = data.conversation.messages.at(-1);
      setRunning(false, lastMessage?.role === "error");
    }
  } catch (error) {
    showError(error);
  }
}

function clearConversation(project) {
  state.currentConversation = null;
  elements.chatTitle.textContent = project?.name || "选择或创建项目";
  elements.workspacePath.textContent = project?.path || "每个项目对应一个独立的本地工作区";
  elements.messageInput.disabled = !project;
  elements.sendButton.disabled = !project;
  renderMessages();
  renderTrace([]);
}

function renderMessages() {
  elements.messageList.replaceChildren();
  const messages = state.currentConversation?.messages || [];
  if (!messages.length) {
    const section = document.createElement("section");
    section.className = "empty-state";
    const icon = document.createElement("div");
    icon.className = "empty-icon";
    icon.textContent = "</>";
    const title = document.createElement("h2");
    title.textContent = "从一个真实编程任务开始";
    const text = document.createElement("p");
    text.textContent = "描述预期结果和验收方式。智能体会在当前项目工作区中读取、修改并验证代码。";
    section.append(icon, title, text);
    elements.messageList.append(section);
    return;
  }
  for (const message of messages) {
    elements.messageList.append(messageElement(message));
  }
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
}

function messageElement(message) {
  const article = document.createElement("article");
  article.className = `message ${message.role}`;
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = { user: "YOU", assistant: "AGENT", error: "ERROR" }[message.role] || message.role;
  const content = document.createElement("div");
  content.className = "message-content";
  content.textContent = message.content;
  article.append(label, content);
  if (message.metadata?.iterations) {
    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = `完成 · ${message.metadata.iterations} 轮 · ${message.metadata.stop_reason}`;
    article.append(meta);
  }
  return article;
}

function renderSavedTrace() {
  const messages = state.currentConversation?.messages || [];
  const lastWithEvents = [...messages].reverse().find((message) => message.events?.length);
  renderTrace(lastWithEvents?.events || []);
}

function renderTrace(events) {
  elements.traceList.replaceChildren();
  if (!events.length) {
    const placeholder = document.createElement("div");
    placeholder.className = "trace-placeholder";
    placeholder.textContent = "工具调用、错误和循环轮次会显示在这里。";
    elements.traceList.append(placeholder);
    return;
  }
  for (const event of events) {
    elements.traceList.append(traceElement(event));
  }
  elements.traceList.scrollTop = elements.traceList.scrollHeight;
}

function traceElement(event) {
  const container = document.createElement("div");
  const okClass = event.kind === "tool_end" ? (event.payload.ok ? " ok" : " fail") : "";
  container.className = `trace-event${okClass}`;
  const head = document.createElement("div");
  head.className = "trace-event-head";
  const title = document.createElement("div");
  let titleText = event.kind;
  if (event.kind === "iteration") titleText = `第 ${event.payload.number} 轮`;
  if (event.kind === "tool_start") titleText = `调用 ${event.payload.name}`;
  if (event.kind === "tool_end") titleText = `${event.payload.name} · ${event.payload.ok ? "成功" : "失败"}`;
  if (event.kind === "assistant") titleText = "模型阶段说明";
  title.textContent = titleText;
  const time = document.createElement("span");
  time.textContent = event.created_at ? formatTime(event.created_at) : "";
  head.append(title, time);
  container.append(head);

  let detail = null;
  if (event.kind === "tool_start") detail = event.payload.arguments;
  if (event.kind === "tool_end") detail = parseToolResult(event.payload.result);
  if (event.kind === "assistant") detail = event.payload.text;
  if (detail !== null && detail !== undefined) {
    const pre = document.createElement("pre");
    pre.textContent = typeof detail === "string" ? detail : JSON.stringify(detail, null, 2);
    container.append(pre);
  }
  return container;
}

function parseToolResult(result) {
  try {
    return JSON.parse(result);
  } catch {
    return result;
  }
}

async function createProject(event) {
  event.preventDefault();
  if (event.submitter?.value === "cancel") {
    elements.projectDialog.close();
    return;
  }
  try {
    const project = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name: elements.projectName.value }),
    });
    elements.projectDialog.close();
    elements.projectForm.reset();
    await refreshProjects(project.id);
    await selectProject(project.id);
  } catch (error) {
    showError(error);
  }
}

async function createConversation() {
  if (!state.selectedProjectId) return;
  try {
    const conversation = await api(`/api/projects/${state.selectedProjectId}/conversations`, {
      method: "POST",
      body: JSON.stringify({ title: "新对话" }),
    });
    await refreshProjects(state.selectedProjectId);
    await selectConversation(conversation.id);
  } catch (error) {
    showError(error);
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const content = elements.messageInput.value.trim();
  if (!content || !state.selectedConversationId || state.activeJobId) return;
  try {
    elements.messageInput.value = "";
    setRunning(true);
    const job = await api(`/api/conversations/${state.selectedConversationId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
    state.activeJobId = job.id;
    await reloadCurrentConversation();
    renderTrace(job.events);
    pollJob();
  } catch (error) {
    setRunning(false, true);
    showError(error);
  }
}

async function pollJob() {
  if (!state.activeJobId) return;
  try {
    const job = await api(`/api/jobs/${state.activeJobId}`);
    renderTrace(job.events);
    if (job.status === "running") {
      state.pollTimer = window.setTimeout(pollJob, 700);
      return;
    }
    const failed = job.status === "failed";
    state.activeJobId = null;
    setRunning(false, failed);
    await refreshProjects(state.selectedProjectId);
    await reloadCurrentConversation();
    if (failed) showError(new Error(job.error));
  } catch (error) {
    state.activeJobId = null;
    setRunning(false, true);
    showError(error);
  }
}

function setRunning(running, failed = false) {
  elements.messageInput.disabled = running || !state.selectedConversationId;
  elements.sendButton.disabled = running || !state.selectedConversationId;
  elements.runState.textContent = running ? "运行中" : failed ? "失败" : "空闲";
  elements.runState.className = `run-state ${running ? "running" : failed ? "failed" : "idle"}`;
}

async function reloadCurrentConversation() {
  if (state.selectedConversationId) await selectConversation(state.selectedConversationId);
}

async function refreshProjects(preferredProjectId = null) {
  const data = await api("/api/bootstrap");
  state.projects = data.projects;
  renderRuntime(data.runtime);
  renderProjects();
  const projectId = preferredProjectId || state.selectedProjectId;
  if (projectId) {
    state.selectedProjectId = projectId;
    renderProjects();
    renderConversations();
  }
}

function selectedProject() {
  return state.projects.find((project) => project.id === state.selectedProjectId) || null;
}

function createPlaceholder(text) {
  const element = document.createElement("div");
  element.className = "trace-placeholder";
  element.textContent = text;
  return element;
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

let toastTimer = null;
function showError(error) {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = error instanceof Error ? error.message : String(error);
  elements.toast.classList.add("visible");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 5000);
}

elements.createProjectButton.addEventListener("click", () => {
  elements.projectDialog.showModal();
  window.setTimeout(() => elements.projectName.focus(), 0);
});
elements.projectForm.addEventListener("submit", createProject);
elements.newConversationButton.addEventListener("click", createConversation);
elements.composer.addEventListener("submit", sendMessage);
elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

bootstrap();
