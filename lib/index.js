/**
 * dsh-plugin-qqsend — 「向指定 QQ 群/好友发送单条消息」的 DSH 工具集。
 *
 * ## 与 dsh-plugin-qqchat 的边界
 *
 * qqchat 是**只读**的（解密、解析、定位媒体，完全不碰网络）；qqsend 是**只写**的。
 * 两者刻意拆成两个独立 bundle：只读那边"绝不发消息"的可信承诺，不能被写能力污染。
 *
 * ## 红线（写死在代码里）
 *
 * 一次调用只发**一条**（没有批量/群发参数）；不做自动回复；不做频率规避；
 * 不注入 QQ 进程、不改 QQ 安装包；不重实现 QQ 协议。
 * 真实发送有三道锁：配置 `safety.allowSend`、调用时 `confirm=true`、以及可选目标白名单，
 * 外加限速与审计。
 *
 * ## 为什么一个宿主包都不 import
 *
 * 按绝对路径挂载的工作区插件没有可达的 `node_modules`；而把 `dsh-tools` 声明成真依赖
 * 会带进第二份 cordis，进程里出现第二个工具注册表。所以只用 Node 内置模块。
 *
 * @module dsh-plugin-qqsend
 */
import { existsSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { scheduleBootstrap } from "./bootstrap.js";

/** Cordis 插件身份。 */
export const name = "plugin-qqsend";

/** 激活前必须就绪的服务。 */
export const inject = ["tools", "subprocess"];

const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_GRACE_MS = 5_000;
const DEFAULT_MAX_OUTPUT_BYTES = 400_000;
const MAX_TIMER_DELAY_MS = 2_147_483_647;

/** 这个插件所在的包目录（`<pkg>/lib/index.js` → `<pkg>`）。 */
const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/** 强制 UTF-8：Windows 上捆绑解释器默认 cp936，中文会乱码。 */
const BASE_ENV = {
	PYTHONIOENCODING: "utf-8",
	PYTHONUTF8: "1",
	PYTHONUNBUFFERED: "1",
	PYTHONDONTWRITEBYTECODE: "1"
};

function harnessHome() {
	return process.env.DSH_HOME ?? join(homedir(), ".dsh");
}

function payloadInterpreter(root) {
	return process.platform === "win32"
		? join(root, "dependencies", "python", "python.exe")
		: join(root, "dependencies", "python", "bin", "python3");
}

async function bundledPython() {
	const directory = process.env.DSH_RUNTIMES ?? join(harnessHome(), "dsh-runtimes");
	let entries;
	try {
		entries = await readdir(directory, { withFileTypes: true });
	} catch {
		return undefined;
	}
	const candidates = [];
	for (const entry of entries) {
		if (!entry.isDirectory()) continue;
		const root = join(directory, entry.name);
		let manifest;
		try {
			manifest = JSON.parse(await readFile(join(root, "runtime.json"), "utf8"));
		} catch {
			manifest = undefined;
		}
		if (manifest?.platform !== undefined && manifest.platform !== process.platform) continue;
		if (manifest?.arch !== undefined && manifest.arch !== process.arch) continue;
		const interpreter = payloadInterpreter(root);
		if (!existsSync(interpreter)) continue;
		candidates.push({ interpreter, ...(typeof manifest.python === "string" ? { version: manifest.python } : {}) });
	}
	candidates.sort((left, right) => left.interpreter.localeCompare(right.interpreter));
	return candidates[0];
}

function readStream(reader) {
	const read = reader.readFrom(0);
	return {
		text: read.text,
		truncated: read.lossy,
		...(read.spillPath !== undefined ? { spillPath: read.spillPath } : {})
	};
}

function parseJsonOutput(text) {
	const trimmed = (text ?? "").trim();
	if (trimmed.length === 0) return undefined;
	try {
		return JSON.parse(trimmed);
	} catch {
		const start = trimmed.lastIndexOf("\n{");
		if (start >= 0) {
			try {
				return JSON.parse(trimmed.slice(start + 1));
			} catch {
				return undefined;
			}
		}
		return undefined;
	}
}

const STREAM_SCHEMA = {
	type: "object",
	additionalProperties: false,
	properties: {
		text: { type: "string" },
		truncated: { type: "boolean" },
		spillPath: { type: "string", description: "流太长被截断时，完整内容的落盘路径。" }
	},
	required: ["text", "truncated"]
};

const RESULT_SCHEMA = {
	type: "object",
	additionalProperties: false,
	properties: {
		ok: { type: "boolean" },
		command: { type: "string" },
		exitCode: { oneOf: [{ type: "integer" }, { type: "null" }] },
		timedOut: { type: "boolean" },
		aborted: { type: "boolean" },
		elapsedMs: { type: "number" },
		error: { type: "string" },
		data: { type: "object" },
		stdout: STREAM_SCHEMA,
		stderr: STREAM_SCHEMA
	},
	required: ["ok", "command", "exitCode", "timedOut", "aborted", "elapsedMs", "stdout", "stderr"]
};

/** 渲染 doctor。 */
function renderDoctor(data) {
	const lines = [`通道：${data.transport}｜${data.ok ? "体检通过" : "体检有问题"}`];
	for (const check of data.checks ?? []) {
		const mark = check.ok ? "OK  " : check.level === "error" ? "FAIL" : "WARN";
		lines.push(`${mark} ${check.name}：${check.detail}`);
		if (!check.ok && check.hint) lines.push(`     → ${check.hint}`);
	}
	const g = data.guard ?? {};
	lines.push(
		"",
		`发送总开关：${g.allowSend ? "已打开" : "关闭（只能 dry-run）"}`,
		`白名单：${(g.allowlist ?? []).length > 0 ? g.allowlist.join("、") : "（未限制）"}`,
		`限速：${g.maxPerMinute} 条/分钟，同一目标 ${g.maxPerTargetPerHour} 条/小时`,
		`最近：1 分钟内 ${g.sentLastMinute} 条 / 1 小时内 ${g.sentLastHour} 条`,
		`审计：${g.auditLog}`
	);
	return lines.join("\n");
}

/** 渲染 targets。 */
function renderTargets(data) {
	const items = data.targets ?? [];
	if (items.length === 0) return `没有匹配「${data.query ?? ""}」的目标（可发送目标来自 qqchat 的解密库）`;
	return [
		`匹配 ${data.count} 个目标（群 ${data.byKind?.group ?? 0} / 好友 ${data.byKind?.friend ?? 0}）：`,
		...items.map((t) => `- [${t.kind}] ${t.id}｜${t.name || "(无名)"}`)
	].join("\n");
}

/** 渲染 send 结果——这是最需要说清楚的：到底发了没有。 */
function renderSend(data) {
	const lines = [];
	const target = data.target ?? {};
	const would = data.wouldSend ?? {};
	if (data.sent) {
		lines.push(`已发送 → [${target.kind}] ${target.name || ""} ${target.id}`);
		lines.push(`内容：${would.text ?? ""}`);
		lines.push(`通道：${data.result?.transport}｜${data.result?.detail ?? ""}`);
	} else {
		lines.push(`未发送${data.dryRun ? "（dry-run）" : ""}`);
		lines.push(`目标：[${target.kind}] ${target.name || ""} ${target.id}（${data.resolveReason ?? ""}）`);
		lines.push(`内容：${would.text ?? ""}`);
		if (data.error) lines.push(`原因：${data.error}`);
		if (data.note) lines.push(`说明：${data.note}`);
	}
	const checks = data.guard?.checks ?? [];
	if (checks.length > 0) {
		lines.push("", "闸门检查：");
		for (const c of checks) lines.push(`- ${c.ok ? "OK" : "NO"} ${c.name}：${c.detail}`);
	}
	if (data.candidates?.length) {
		lines.push("", "候选目标（请用更精确的名字或号码）：");
		for (const c of data.candidates) lines.push(`- [${c.kind}] ${c.id}｜${c.name || ""}`);
	}
	return lines.join("\n");
}

/** 渲染审计历史。 */
function renderHistory(data) {
	const entries = data.entries ?? [];
	if (entries.length === 0) return "还没有发送记录。";
	return [
		`最近 ${data.count} 条发送记录：`,
		...entries.map(
			(e) =>
				`- ${e.time}｜${e.dryRun ? "[dry-run] " : ""}${e.ok ? "成功" : "失败"}｜` +
				`[${e.kind}] ${e.targetName || ""} ${e.target}｜${e.elements ?? e.textLength} 字｜${e.transport}`
		)
	].join("\n");
}

/**
 * 渲染登录二维码。
 *
 * 关键点：把 Markdown 图片原样放进返回文本，DSH 聊天界面会把它渲染成图片，
 * 于是二维码直接在 GUI 页面里就能扫 —— 不用再去翻 NapCat 的控制台。
 */
function renderQrcode(data) {
	if (data.loggedIn) {
		return [`已登录：${data.login?.reason ?? ""}`, "", data.nextAction ? `下一步：${data.nextAction}` : ""]
			.filter(Boolean)
			.join("\n");
	}
	if (!data.ok) {
		const lines = [`取二维码失败：${data.error ?? "未知原因"}`];
		if (data.searched?.length) {
			lines.push("", "找过这些位置：");
			for (const p of data.searched) lines.push(`- ${p}`);
		}
		if (data.nextAction) lines.push("", `下一步：${data.nextAction}`);
		return lines.join("\n");
	}
	const lines = [];
	if (data.markdown) lines.push(data.markdown, "");
	lines.push(`二维码图片：${data.imagePath}`);
	lines.push(`生成于 ${data.ageSeconds} 秒前${data.stale ? "（**可能已过期，重跑本工具即可刷新**）" : ""}`);
	if (data.nextAction) lines.push("", `下一步：${data.nextAction}`);
	return lines.join("\n");
}

/**
 * 渲染媒体自动下载结果。
 */
function renderFetchMedia(data) {
	if (!data.ok) {
		return [`下载失败：${data.error ?? "未知原因"}`, data.nextAction ? `下一步：${data.nextAction}` : ""].filter(Boolean).join("\n");
	}
	const lines = [
		`下载目录：${data.dest}（${data.destSource ?? ""}）`,
		`扫描 ${(data.conversations ?? []).length} 个会话 / ${data.scannedMessages} 条消息｜本轮下载 ${data.downloadedCount} 个文件` +
			(data.watch ? `｜轮询 ${data.rounds} 轮，共 ${data.elapsedSeconds}s` : "")
	];
	const files = data.files ?? [];
	if (files.length > 0) {
		lines.push("", "本轮拿到的文件：");
		for (const f of files.slice(0, 30)) {
			lines.push(`- [${f.type}] ${f.file}${f.size ? `（${f.size} 字节）` : ""}${f.note ? ` — ${f.note}` : ""}`);
		}
	}
	const errors = data.errors ?? [];
	if (errors.length > 0) {
		lines.push("", `没拿到来源的 ${errors.length} 项（如实列出，不静默丢弃）：`);
		for (const e of errors.slice(0, 10)) lines.push(`- ${e}`);
	}
	if (data.nextAction) lines.push("", `下一步：${data.nextAction}`);
	return lines.join("\n");
}

function renderData(command, data) {
	if (command === "doctor") return renderDoctor(data);
	if (command === "targets") return renderTargets(data);
	if (command === "send") return renderSend(data);
	if (command === "history") return renderHistory(data);
	if (command === "qrcode") return renderQrcode(data);
	if (command === "fetch-media") return renderFetchMedia(data);
	return JSON.stringify(data, null, 2).slice(0, 4000);
}

function renderResult(value) {
	const sections = [];
	if (value.error && value.data?.command !== "send") sections.push(`[失败] ${value.error}`);
	const data = value.data;
	if (data && typeof data === "object") sections.push(renderData(value.command, data));
	const stderr = value.stderr?.text?.trim();
	if (stderr) sections.push(`[stderr]\n${stderr}`);
	if (value.timedOut) sections.push(`[超时] 超过 ${value.timeoutMs ?? "?"} ms 被终止`);
	else if (value.exitCode !== 0 && value.exitCode !== null) sections.push(`[exit code: ${value.exitCode}]`);
	return [{ type: "text", text: sections.filter(Boolean).join("\n\n") }];
}

function cliTool(spec, hooks) {
	return {
		name: spec.name,
		description: spec.description,
		parameters: spec.parameters,
		output: { schema: RESULT_SCHEMA, render: (_args, value) => renderResult(value) },
		async execute(args, exec) {
			const { command, argv } = spec.build(args ?? {});
			return hooks.run(command, argv, args ?? {}, exec);
		},
		presentCall: (args) => ({
			card: "terminal",
			title: spec.title ?? spec.name,
			description: spec.present ? spec.present(args ?? {}) : spec.name
		})
	};
}

/**
 * 注册 qqsend 工具。
 * @param ctx - 提供 `tools`（必需）与 `subprocess`。
 * @param config - 插件配置（见 cordis.patch.yml）。
 */
export function apply(ctx, config = {}) {
	scheduleBootstrap(config);
	const engineDir = config.engineDir === undefined ? join(PACKAGE_ROOT, "engine") : String(config.engineDir);
	const configuredPython = config.pythonPath === undefined
		? (process.env.QQSEND_PYTHON || process.env.DSH_PYTHON || undefined)
		: String(config.pythonPath);
	const configuredConfig = config.configPath === undefined ? undefined : String(config.configPath);
	const configuredTransport = config.transport === undefined ? undefined : String(config.transport);
	const defaultTimeoutMs = Number.isFinite(config.timeoutMs) ? Number(config.timeoutMs) : DEFAULT_TIMEOUT_MS;
	const graceMs = Number.isFinite(config.graceMs) ? Number(config.graceMs) : DEFAULT_GRACE_MS;
	const maxOutputBytes = Number.isFinite(config.maxOutputBytes) ? Number(config.maxOutputBytes) : DEFAULT_MAX_OUTPUT_BYTES;

	if (!existsSync(engineDir)) throw new Error(`plugin-qqsend: engineDir 不存在：${engineDir}`);

	let discovered;
	const resolvePython = async (signal) => {
		if (configuredPython !== undefined) return { interpreter: await ctx.subprocess.resolveExecutable(configuredPython, BASE_ENV, signal) };
		discovered ??= await bundledPython();
		if (discovered !== undefined) return discovered;
		for (const candidate of (process.platform === "win32" ? ["python", "py", "python3"] : ["python3", "python"])) {
			try {
				return { interpreter: await ctx.subprocess.resolveExecutable(candidate, BASE_ENV, signal) };
			} catch (error) {
				if (signal?.aborted) throw error;
			}
		}
		throw new Error("plugin-qqsend: 找不到 Python，请安装 Python 3.11+，或在 config.pythonPath / QQSEND_PYTHON / DSH_PYTHON 指定解释器");
	};

	const run = async (command, args, raw, exec) => {
		const startedAt = Date.now();
		const timeoutMs = Number.isFinite(raw.timeoutMs) ? Number(raw.timeoutMs) : defaultTimeoutMs;
		if (timeoutMs > MAX_TIMER_DELAY_MS) throw new Error(`plugin-qqsend: timeoutMs 不能大于 ${MAX_TIMER_DELAY_MS}`);
		const python = await resolvePython(exec.signal);

		const argv = [python.interpreter, "-m", "qqsend", "--json"];
		if (configuredConfig !== undefined) argv.push("--config", configuredConfig);
		const transport = typeof raw.transport === "string" && raw.transport.length > 0 ? raw.transport : configuredTransport;
		if (transport !== undefined) argv.push("--transport", transport);
		argv.push(command, ...args);

		const env = { ...BASE_ENV, PYTHONPATH: engineDir };
		let sandbox;
		const policy = ctx.get?.("sandboxPolicy")?.resolve(exec.agent === undefined ? {} : { session: exec.agent.session });
		let finalArgv = argv;
		if (policy !== undefined && policy.mode !== "danger-full-access") {
			const confiner = ctx.get?.("sandbox");
			if (confiner === undefined) throw new Error(`plugin-qqsend: 当前会话运行在 ${policy.mode} 模式但没有沙箱提供者，拒绝无隔离执行`);
			const confined = await confiner.confine(argv, { mode: policy.mode, workspaceRoot: policy.workspaceRoot }, exec.signal);
			finalArgv = confined.argv;
			sandbox = { mode: policy.mode, enforcement: confined.enforcement };
		}

		let handle;
		try {
			handle = ctx.subprocess.spawn({
				argv: finalArgv,
				cwd: engineDir,
				stdio: {
					stdin: "ignore",
					stdout: { maxBytes: maxOutputBytes, spill: { maxBytes: maxOutputBytes * 8 } },
					stderr: { maxBytes: maxOutputBytes, spill: { maxBytes: maxOutputBytes * 8 } }
				},
				graceMs,
				signal: exec.signal,
				env
			});
		} catch (error) {
			throw new Error(`plugin-qqsend: 无法启动 ${python.interpreter}：${String(error)}`);
		}

		let timedOut = false;
		const timer = setTimeout(() => {
			timedOut = true;
			try {
				handle.terminate();
			} catch {
				/* 已结束 */
			}
		}, timeoutMs);
		let outcome;
		try {
			outcome = await handle.done;
		} finally {
			clearTimeout(timer);
		}
		const stdout = readStream(handle.collected.stdout);
		const stderr = readStream(handle.collected.stderr);
		const payload = parseJsonOutput(stdout.text);
		const exitCode = outcome.exitCode === undefined ? null : outcome.exitCode;
		const failed = payload !== undefined && payload.ok === false && typeof payload.error === "string";
		return {
			ok: !timedOut && exitCode === 0 && payload !== undefined && payload.ok !== false,
			command,
			exitCode,
			timedOut,
			aborted: exec.signal.aborted,
			elapsedMs: Date.now() - startedAt,
			...(failed ? { error: payload.error } : {}),
			...(payload !== undefined ? { data: payload } : {}),
			...(sandbox !== undefined ? { sandbox } : {}),
			stdout,
			stderr
		};
	};

	// --------------------------------------------------------------------- //
	// 工具定义
	// --------------------------------------------------------------------- //

	ctx.tools.register(
		cliTool(
			{
				name: "qq_send_doctor",
				title: "QQ 发送通道体检",
				description:
					"检查发送通道是否可用：OneBot（NapCat/LLOneBot）是否在线、**登录的是哪个 QQ 号**、" +
					"发送总开关（safety.allowSend）、目标白名单、限速额度、审计日志位置、以及可发送目标索引。 " +
					"任何发送失败先跑它。注意：本插件是**写**能力，与只读的 qqchat 完全独立。",
				parameters: { type: "object", additionalProperties: false, properties: {} },
				build: () => ({ command: "doctor", argv: [] })
			},
			{ run }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "qq_send_targets",
				title: "查找可发送目标",
				description:
					"按群号/群名/好友号/好友名搜索可发送目标。目标索引来自 **qqchat 解密出来的只读库**， " +
					"所以能按「群名」这种名字找人，避免手打号码打错。返回 kind（group/friend）与 id。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						query: { type: "string", description: "群号 / 群名 / 好友号 / 好友名（可留空列出全部）。" },
						limit: { type: "number", description: "最多返回多少条（默认 50）。" },
						transport: { type: "string", description: "覆盖通道：onebot | mock。" }
					}
				},
				build: (args) => {
					const argv = [];
					if (typeof args.query === "string" && args.query.length > 0) argv.push("--query", args.query);
					if (Number.isFinite(args.limit)) argv.push("--limit", String(args.limit));
					return { command: "targets", argv };
				}
			},
			{ run }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "qq_send",
				title: "发送 QQ 消息（支持表情 / 贴图 / 连发 / 引用）",
				description:
					"向指定的**一个**群或好友发送消息。默认是 dry-run（只回显将发送什么，不发送）； " +
					"真实发送需要同时满足：① 配置里 safety.allowSend=true；② 本次调用 confirm=true； " +
					"③ 目标在白名单内（若配置了白名单）；④ 未触发限速。" +
					"目标可以用群号/群名/好友号/好友名，解析到多个时**拒绝发送并列出候选**，绝不替你猜。 " +
					"**一次调用只针对一个目标**，没有群发参数。\n" +
					"支持四类附加能力（都由调用方显式指定，不猜）：\n" +
					"· `faces` 系统表情（小黄脸）：传编号或名字，如 [\"22\",\"微笑\"]。QQ 不认的编号会被丢弃并告知——" +
					"实测发一个不存在的编号会让**整条消息被拒**，所以这里做了白名单校验。\n" +
					"· `stickers` 贴图表情包：传 URL；也可以传 \"latest\" 复用该会话最近出现过的表情包。" +
					"（实测表情包 = image 段 + sub_type=1，不是 mface。）\n" +
					"· `messages` 连发：传字符串数组，会一条一条按顺序发出，中间停 delay_ms 毫秒。\n" +
					"· `quote` 引用：传某条消息的 message_id，回复时带上引用。\n" +
					"发送结果写入审计日志（只记长度/哈希/节选，表情与贴图只记数量）。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						to: { type: "string", description: "目标：群号 / 群名 / 好友号 / 好友名（必填）。" },
						text: { type: "string", description: "正文；可以留空（只发表情/贴图时）。" },
						kind: { type: "string", enum: ["auto", "group", "friend"], description: "目标类型，默认 auto 自动判断。" },
						faces: { type: "array", items: { type: "string" }, description: '系统表情，编号或名字，如 ["22","微笑"]。认不出的会被丢弃。' },
						stickers: { type: "array", items: { type: "string" }, description: '贴图表情包 URL；传 ["latest"] 表示复用该会话最近一个。' },
						messages: { type: "array", items: { type: "string" }, description: "连发：按顺序逐条发送；给了它就忽略 text 的正文。" },
						quote: { type: "string", description: "要引用的 message_id。" },
						delay_ms: { type: "number", description: "连发之间的间隔毫秒，默认 900。" },
						dry_run: { type: "boolean", description: "只回显不发送（默认行为；显式传 true 也一样）。" },
						confirm: { type: "boolean", description: "确认真实发送。缺省 = 只做 dry-run，绝不发送。" },
						transport: { type: "string", description: "覆盖通道：onebot | mock。" },
						timeoutMs: { type: "number", description: `超时毫秒数，默认 ${defaultTimeoutMs}。` }
					},
					required: ["to"]
				},
				build: (args) => {
					const argv = ["--to", String(args.to)];
					if (typeof args.text === "string" && args.text.length > 0) argv.push("--text", args.text);
					if (typeof args.kind === "string" && args.kind.length > 0) argv.push("--kind", args.kind);
					if (Array.isArray(args.faces) && args.faces.length > 0) argv.push("--faces", args.faces.join(","));
					if (Array.isArray(args.stickers) && args.stickers.length > 0) argv.push("--stickers", args.stickers.join(","));
					for (const m of args.messages ?? []) argv.push("--messages", String(m));
					if (typeof args.quote === "string" && args.quote.length > 0) argv.push("--quote", args.quote);
					if (Number.isFinite(args.delay_ms)) argv.push("--delay-ms", String(args.delay_ms));
					if (args.dry_run === true) argv.push("--dry-run");
					if (args.confirm === true) argv.push("--confirm");
					return { command: "send", argv };
				},
				present: (args) => {
					const bits = [];
					if (args.faces?.length) bits.push(`表情${args.faces.length}`);
					if (args.stickers?.length) bits.push(`贴图${args.stickers.length}`);
					if (args.messages?.length) bits.push(`连发${args.messages.length}条`);
					if (args.quote) bits.push("引用");
					const mode = args.confirm === true && args.dry_run !== true ? "真实发送" : "dry-run";
					return `${mode} → ${args.to ?? "?"}${bits.length ? "（" + bits.join("、") + "）" : ""}`;
				}
			},
			{ run }
		)
	);

	// 定时发送
	ctx.tools.register(
		cliTool(
			{
				name: "qq_schedule",
				title: "定时发送 QQ 消息",
				description:
					"把一条消息交给钟表：到点自动发。支持 add / list / remove / run 四个动作。\n" +
					"· 触发方式二选一：`every`（每隔 N 分钟，下限 5 分钟）或 `at`（每天 HH:MM）。\n" +
					"· 内容同样支持 `faces` / `stickers` / `messages`（连发）。\n" +
					"· **定时任务不是绕过安全的后门**：每次真的发送前照样过发送闸门" +
					"（allowSend / 白名单 / 限速 / 审计）。\n" +
					"· `run` 默认 dry-run，只有显式 confirm=true 才真发。\n" +
					"注意：本插件自身不带常驻进程，`run` 需要被调用才会检查到点任务；" +
					"要真正无人值守，请把 `python -m qqsend schedule run --confirm` 挂到你自己的计划任务里。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						action: { type: "string", enum: ["add", "list", "remove", "run"], description: "要做的动作。" },
						id: { type: "string", description: "计划 id（remove 用）。" },
						to: { type: "string", description: "目标：群号 / 群名 / 好友号 / 好友名。" },
						kind: { type: "string", enum: ["auto", "group", "friend"], description: "目标类型。" },
						text: { type: "string", description: "要发的内容。" },
						faces: { type: "array", items: { type: "string" }, description: "系统表情，编号或名字。" },
						stickers: { type: "array", items: { type: "string" }, description: "贴图表情包 URL。" },
						every: { type: "number", description: "每隔多少分钟发一次（下限 5）。" },
						at: { type: "string", description: "每天几点发，HH:MM。" },
						enabled: { type: "boolean", description: "是否启用，默认 true。" },
						confirm: { type: "boolean", description: "run 时确认真发（缺省=dry-run）。" },
						timeoutMs: { type: "number", description: `超时毫秒数，默认 ${defaultTimeoutMs}。` }
					},
					required: ["action"]
				},
				build: (args) => {
					const argv = [String(args.action)];
					if (typeof args.id === "string" && args.id.length > 0) argv.push("--id", args.id);
					if (typeof args.to === "string" && args.to.length > 0) argv.push("--to", args.to);
					if (typeof args.kind === "string" && args.kind.length > 0) argv.push("--kind", args.kind);
					if (typeof args.text === "string" && args.text.length > 0) argv.push("--text", args.text);
					if (Array.isArray(args.faces) && args.faces.length > 0) argv.push("--faces", args.faces.join(","));
					if (Array.isArray(args.stickers) && args.stickers.length > 0) argv.push("--stickers", args.stickers.join(","));
					if (Number.isFinite(args.every)) argv.push("--every", String(args.every));
					if (typeof args.at === "string" && args.at.length > 0) argv.push("--at", args.at);
					if (args.enabled === false) argv.push("--enabled", "false");
					if (args.confirm === true) argv.push("--confirm");
					return { command: "schedule", argv };
				},
				present: (args) => `定时任务 ${args.action}${args.to ? " → " + args.to : ""}`
			},
			{ run }
		)
	);

	// 表情包查询
	ctx.tools.register(
		cliTool(
			{
				name: "qq_stickers",
				title: "查询可用表情包",
				description:
					"列出可用的 QQ 贴图表情包 URL，配合 qq_send 的 `stickers` 参数使用。\n" +
					"· `source=recent`（默认）：该会话**最近出现过**的表情包——用别人的贴图回击最自然。\n" +
					"· `source=collection`：本账号**收藏**的表情包。\n" +
					"实测 QQ 表情包在消息里是 image 段 + sub_type=1（不是 mface），本工具返回的 URL 可直接发送。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						source: { type: "string", enum: ["recent", "collection"], description: "recent=会话里最近出现的；collection=账号收藏的。" },
						conversation: { type: "string", description: "group:123 或 friend:456（source=recent 时必填）。" },
						limit: { type: "number", description: "最多返回多少个，默认 20。" },
						timeoutMs: { type: "number", description: `超时毫秒数，默认 ${defaultTimeoutMs}。` }
					},
					required: []
				},
				build: (args) => {
					const argv = [];
					if (typeof args.source === "string" && args.source.length > 0) argv.push("--source", args.source);
					if (typeof args.conversation === "string" && args.conversation.length > 0) argv.push("--conversation", args.conversation);
					if (Number.isFinite(args.limit)) argv.push("--limit", String(args.limit));
					return { command: "stickers", argv };
				},
				present: (args) => `查询表情包（${args.source ?? "recent"}）`
			},
			{ run }
		)
	);

	// 上传文件
	ctx.tools.register(
		cliTool(
			{
				name: "qq_send_file",
				title: "上传文件到 QQ 群 / 好友",
				description:
					"把**一个文件**上传到指定的一个群或好友。由你（DSH）决定传哪个文件。\n" +
					"· `file` 传**本地绝对路径**（如 `C:\\\\Users\\\\me\\\\report.pdf`）或 http(s) URL。\n" +
					"· 上传前会做体检：文件必须存在、**必须是普通文件**（目录会被拒），" +
					"并回报文件名与大小；超过 100MB 会给出提醒但不阻止。\n" +
					"· 真实上传同样要过发送闸门：`safety.allowSend=true` + 本次 `confirm=true` + 白名单 + 限速，" +
					"并写入审计（只记文件名与大小，**不记本机完整路径**）。\n" +
					"· 群文件可传 `folder` 指定目录 id（省略则传根目录）。\n" +
					"目标解析到多个时**拒绝发送并列出候选**，绝不替你猜。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						to: { type: "string", description: "目标：群号 / 群名 / 好友号 / 好友名（必填）。" },
						file: { type: "string", description: "本地绝对路径或 http(s) URL（必填）。" },
						name: { type: "string", description: "显示给对方的文件名；省略则用原文件名。" },
						folder: { type: "string", description: "群文件目录 id（仅群有效，可省略）。" },
						kind: { type: "string", enum: ["auto", "group", "friend"], description: "目标类型，默认 auto。" },
						dry_run: { type: "boolean", description: "只回显不发送（默认行为）。" },
						confirm: { type: "boolean", description: "确认真实上传。缺省 = 只做 dry-run，绝不上传。" },
						transport: { type: "string", description: "覆盖通道：onebot | mock。" },
						timeoutMs: { type: "number", description: `超时毫秒数，默认 ${defaultTimeoutMs}。` }
					},
					required: ["to", "file"]
				},
				build: (args) => {
					const argv = ["--to", String(args.to), "--file", String(args.file)];
					if (typeof args.name === "string" && args.name.length > 0) argv.push("--name", args.name);
					if (typeof args.folder === "string" && args.folder.length > 0) argv.push("--folder", args.folder);
					if (typeof args.kind === "string" && args.kind.length > 0) argv.push("--kind", args.kind);
					if (args.dry_run === true) argv.push("--dry-run");
					if (args.confirm === true) argv.push("--confirm");
					return { command: "send-file", argv };
				},
				present: (args) =>
					`${args.confirm === true && args.dry_run !== true ? "上传" : "dry-run"} ${args.file ?? "?"} → ${args.to ?? "?"}`
			},
			{ run }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "qq_download_media",
				title: "自动下载图片/文件到系统下载目录",
				description:
					"把 QQ 里收到的**图片 / 文件 / 视频 / 语音**自动下载到**系统的「下载」文件夹**" +
					"（目录是动态解析的：走 Windows 的 SHGetKnownFolderPath，你把「下载」改到别的盘也认得）。" +
					"默认**增量**：只下载没下过的，重复调用不会重复下载。" +
					"能拿到直链就走 HTTP 下载，拿不到就问 NapCat 要本地路径再复制（更快）。" +
					"`durationSeconds > 0` 进入**轮询模式**：在指定时长内持续观察，一出现新图片/文件就下载。" +
					"拿不到来源的条目会如实列出，不会静默丢弃。需要 OneBot(NapCat) 在线且已登录。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						count: { type: "number", description: "每条会话拉取多少条历史（默认 50）。" },
						kinds: { type: "string", description: "逗号分隔的类型：image,file,video,record（默认全部）。" },
						conversation: { type: "array", items: { type: "string" }, description: "只扫这些会话（group:123 / friend:456）；省略则自动扫最近会话。" },
						dest: { type: "string", description: "下载目录；省略则用系统「下载」目录。" },
						dry_run: { type: "boolean", description: "只报告发现了什么，不真的下载。" },
						duration_seconds: { type: "number", description: "轮询模式：持续多少秒（0=只扫一次）。" },
						interval: { type: "number", description: "轮询间隔秒（默认 5）。" },
						max_conversations: { type: "number", description: "最多扫描多少个会话（默认 20）。" },
						timeoutMs: { type: "number", description: `超时毫秒数，默认 ${defaultTimeoutMs}。` }
					}
				},
				build: (args) => {
					const argv = [];
					if (Number.isFinite(args.count)) argv.push("--count", String(args.count));
					if (typeof args.kinds === "string" && args.kinds.length > 0) argv.push("--kinds", args.kinds);
					for (const item of args.conversation ?? []) argv.push("--conversation", String(item));
					if (typeof args.dest === "string" && args.dest.length > 0) argv.push("--dest", args.dest);
					if (args.dry_run === true) argv.push("--dry-run");
					if (Number.isFinite(args.duration_seconds)) argv.push("--watch", String(args.duration_seconds));
					if (Number.isFinite(args.interval)) argv.push("--interval", String(args.interval));
					if (Number.isFinite(args.max_conversations)) argv.push("--max-conversations", String(args.max_conversations));
					return { command: "fetch-media", argv };
				},
				present: (args) => (args.duration_seconds ? `轮询下载 ${args.duration_seconds}s` : "下载新收到的图片/文件")
			},
			{ run }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "qq_login_qrcode",
				title: "在 GUI 里显示 QQ 登录二维码",
				description:
					"把 NapCat 的登录二维码取到**聊天页面里直接显示**（返回 Markdown 图片，GUI 会渲染成图），" +
					"不用再去翻 NapCat 的控制台。同时报告当前登录状态：" +
					"已登录就告诉你登录的是哪个 QQ 号；没登录就把二维码图片复制到工作目录并显示出来。" +
					"二维码有短时效，过期了再调用一次本工具即可（NapCat 会刷新）。" +
					"需要配置 `napcat.dir` 指向 NapCat（Shell 版）的安装目录。",
				parameters: { type: "object", additionalProperties: false, properties: {} },
				build: () => ({ command: "qrcode", argv: [] })
			},
			{ run }
		)
	);

	ctx.tools.register(
		cliTool(
			{
				name: "qq_send_history",
				title: "查看发送审计日志",
				description:
					"列出最近的发送记录（时间、目标、成功与否、通道、内容长度与节选）。" +
					"审计日志**不保存完整正文**，只保存长度、哈希与前若干字符。",
				parameters: {
					type: "object",
					additionalProperties: false,
					properties: {
						limit: { type: "number", description: "最多返回多少条（默认 20）。" }
					}
				},
				build: (args) => {
					const argv = [];
					if (Number.isFinite(args.limit)) argv.push("--limit", String(args.limit));
					return { command: "history", argv };
				}
			},
			{ run }
		)
	);
}

export { renderDoctor, renderSend, renderQrcode, renderFetchMedia, parseJsonOutput };
