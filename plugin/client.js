/**
 * dsh-bili-companion —— 浏览器半身（「设置」里的一页）
 * ============================================================================
 * ## 三条本机契约（照抄运行中的样板，不是猜的）
 *
 * 1. **这个文件不是普通模块，是"自注册脚本"**：
 *        window.__ModuleLoader__.load({ id, factory })
 *    `id` 必须与 `cordis.patch.yml` 的 id、index.js 的 `name` 一致。
 * 2. **进「设置」必须走插槽**，别自己糊标题栏按钮 + 全屏弹窗
 *    （竞品第一版这么干，被一眼看出"跟别的插件不一样"）。
 * 3. **输入框必须绑"本地草稿"**：本页每 5 秒去问一次 `/status`，
 *    直接把服务端的值绑到 input 上，会把正在打的字冲掉
 *    （竞品原话：陌生人验收时抓到的）。
 */

window.__ModuleLoader__.load({
  // ★★ 必须**逐字**等于 package.json 的 `name`（= "dsh-bili-companion"）。
  //    写短了（如 "bili-companion"）会让加载器认为"加载了但没注册"，启动即报：
  //      client-modules: bundle .../client.js loaded without registering
  //        "dsh-bili-companion" via ModuleLoader.load
  //    → 插件加载失败 / 进恢复页。
  //    （真机案例：deepseek-harness discussion #5235，作者改名 dsh-vision →
  //      dsh-vision-web 时 bundle id 没跟着改。）
  //    ⚠️ selfcheck.mjs 有断言把这条钉死，别再改短。
  id: 'dsh-bili-companion',
  factory: (require) => {
    const React = require('react')
    const h = React.createElement

    const API = '/api/bili-companion'
    const jget = async (p) => (await fetch(API + p, { credentials: 'same-origin' })).json()
    const jpost = async (p, body) =>
      (
        await fetch(API + p, {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body || {}),
        })
      ).json()

    // 颜色全部走 DSH 主题变量，跟着主题变
    const CSS = `
[data-bc-page]{display:flex;flex-direction:column;gap:18px;padding:2px 2px 8px;max-width:640px;}
[data-bc-sec]{display:flex;flex-direction:column;gap:8px;}
[data-bc-h]{font-size:11px;font-weight:600;letter-spacing:.04em;color:var(--dsw-alias-label-tertiary,#9aa0a6);}
[data-bc-row]{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
[data-bc-grow]{flex:1;min-width:0;}
[data-bc-label]{font-size:13px;}
[data-bc-note]{font-size:11px;line-height:16px;color:var(--dsw-alias-label-tertiary,#9aa0a6);}
[data-bc-sw]{flex:none;width:38px;height:22px;border-radius:11px;border:0;cursor:pointer;position:relative;
  background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.35));transition:background .15s;padding:0;}
[data-bc-sw][data-on]{background:#3b82f6;}
[data-bc-sw] i{position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;
  transition:left .15s;display:block;}
[data-bc-sw][data-on] i{left:19px;}
[data-bc-input]{width:100%;box-sizing:border-box;padding:7px 9px;border-radius:8px;font-size:12px;
  border:1px solid var(--dsw-alias-border-l2,rgba(255,255,255,.14));
  background:var(--dsw-alias-bg-overlay,rgba(128,128,128,.12));color:inherit;font-family:inherit;}
[data-bc-input]:focus{outline:none;border-color:#3b82f6;}
[data-bc-num]{width:88px;text-align:center;}
[data-bc-btn]{padding:7px 12px;border-radius:8px;font-size:12px;cursor:pointer;font-family:inherit;
  border:1px solid var(--dsw-alias-border-l2,rgba(255,255,255,.16));
  background:var(--dsw-alias-bg-overlay,rgba(128,128,128,.14));color:inherit;}
[data-bc-btn]:hover{background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.24));}
[data-bc-btn][data-primary]{background:#3b82f6;border-color:#3b82f6;color:#fff;}
[data-bc-btn][disabled]{opacity:.5;cursor:default;}
[data-bc-msg]{font-size:11px;line-height:16px;padding:7px 9px;border-radius:8px;word-break:break-all;
  background:var(--dsw-alias-bg-overlay,rgba(128,128,128,.12));color:var(--dsw-alias-label-tertiary,#9aa0a6);}
[data-bc-msg][data-good]{color:#22c55e;}
[data-bc-msg][data-warn]{color:#f59e0b;}
[data-bc-badge]{font-size:11px;padding:2px 7px;border-radius:6px;
  background:var(--dsw-alias-bg-overlay,rgba(128,128,128,.14));color:var(--dsw-alias-label-secondary,#c9ccd1);}
[data-bc-pre]{margin:6px 0 0;padding:8px 9px;border-radius:8px;max-height:240px;overflow:auto;font-size:11px;
  line-height:15px;white-space:pre-wrap;word-break:break-all;
  background:var(--dsw-alias-bg-overlay,rgba(0,0,0,.25));color:var(--dsw-alias-label-secondary,#c9ccd1);}
[data-bc-kv]{font-size:11px;line-height:18px;color:var(--dsw-alias-label-tertiary,#9aa0a6);word-break:break-all;}
/* ── 折叠分区（2026-10-17）：字段多了以后，一页铺开太长 ── */
[data-bc-fold]{border:1px solid var(--dsw-alias-border-l2,rgba(255,255,255,.12));border-radius:10px;
  overflow:hidden;background:var(--dsw-alias-bg-overlay,rgba(128,128,128,.05));}
[data-bc-foldhead]{display:flex;align-items:center;gap:8px;width:100%;padding:10px 12px;cursor:pointer;
  border:0;background:transparent;color:inherit;font-family:inherit;font-size:12px;font-weight:600;text-align:left;}
[data-bc-foldhead]:hover{background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.14));}
[data-bc-caret]{flex:none;width:12px;font-size:10px;color:var(--dsw-alias-label-tertiary,#9aa0a6);
  transition:transform .15s;display:inline-block;}
[data-bc-fold][data-open] [data-bc-caret]{transform:rotate(90deg);}
[data-bc-foldbody]{display:flex;flex-direction:column;gap:10px;padding:4px 12px 12px;}
[data-bc-sub]{font-size:11px;color:var(--dsw-alias-label-tertiary,#9aa0a6);font-weight:400;}
[data-bc-field]{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
[data-bc-fieldlabel]{font-size:12px;flex:1;min-width:0;}
[data-bc-fieldnote]{font-size:10px;line-height:14px;color:var(--dsw-alias-label-tertiary,#9aa0a6);}
[data-bc-select]{padding:6px 8px;border-radius:8px;font-size:12px;font-family:inherit;
  border:1px solid var(--dsw-alias-border-l2,rgba(255,255,255,.14));
  background:var(--dsw-alias-bg-overlay,rgba(128,128,128,.12));color:inherit;}
[data-bc-savebar]{display:flex;align-items:center;gap:10px;position:sticky;bottom:0;padding:8px 0;
  background:var(--dsw-alias-bg-base,transparent);}
`

    /**
     * 字段分组（2026-10-17）。
     *
     * 主人要求「防止一页太多，可以将相关的折叠展开进行配置」——
     * 这里按**主题**分组，只把最常用的两组默认展开。
     *
     * ★ `paths` 是点分路径，必须都在 `lib/pyconfig.mjs` 的白名单里
     *   （不在的会被过滤掉，界面上就不会出现"改了没反应"的幽灵字段）。
     */
    const FIELD_GROUPS = [
      {
        title: '什么时候自己跑',
        subtitle: '唤醒方式与频率',
        defaultOpen: true,
        paths: [
          'schedule.mode',
          'schedule.peak_valley',
          'schedule.interval_minutes',
          'schedule.max_runs_per_day',
          'schedule.daily_time',
          'schedule.min_gap_minutes',
        ],
      },
      {
        title: '挑什么、挑多少',
        subtitle: '搜索与筛选',
        defaultOpen: true,
        paths: [
          'discovery.published_within_days',
          'discovery.max_candidates_per_run',
          'discovery.max_full_reads_per_run',
          'discovery.min_score_for_comment',
          // ★ 打分权重（2026-10-17 主人反馈"没什么用的给高分"之后加的）
          'discovery.score_topic_weight',
          'discovery.auto_accept_suggestions',
        ],
      },
      {
        title: '每天最多做多少',
        subtitle: '配额（只影响数量，不影响"能不能发"）',
        defaultOpen: false,
        paths: [
          'interaction.frequency_tier',
          'interaction.max_top_comments_per_day',
          'interaction.max_replies_per_day',
          'interaction.max_replies_per_thread_per_day',
          'interaction.max_likes_per_day',
          'interaction.max_favorites_per_day',
          'interaction.min_action_interval_min',
          'interaction.allow_third_party_threads',
        ],
      },
      {
        title: '人设',
        subtitle: '它怎么说话、允许多大动作',
        defaultOpen: false,
        paths: [
          'persona.display_name',
          'persona.ai_identity',
          'persona.nicknames',
          'persona.enable_liking',
          'persona.enable_favoriting',
          'persona.like_min_score',
          'persona.like_min_not_hype',
          'persona.like_max_per_author_per_day',
          'persona.favorite_min_score',
          'persona.favorite_folders',
          'persona.allow_create_folders',
          'persona.allow_reply_others',
          'persona.allow_follow',
          'persona.allow_negative_feedback',
          'persona.max_items_per_run',
        ],
      },
      {
        title: '豆包（文字处理）',
        subtitle: '不填就只读不分析',
        defaultOpen: false,
        paths: [
          'text_worker.enabled',
          'text_worker.model',
          'text_worker.base_url',
          'text_worker.timeout_seconds',
          'text_worker.max_batch_seconds',
          'text_worker.max_batch_chars',
          'text_worker.max_candidates_to_dsh',
          'text_worker.max_excerpt_chars',
          'text_worker.asr_fallback',
          'text_worker.asr_model',
        ],
      },
    ]

    function Switch({ on, onChange }) {
      return h(
        'button',
        {
          'data-bc-sw': '',
          'data-on': on ? '' : undefined,
          title: on ? '已开' : '已关',
          onClick: () => onChange(!on),
        },
        h('i'),
      )
    }

    function ToggleRow({ label, note, on, onChange }) {
      return h(
        'div',
        { 'data-bc-row': '' },
        h(
          'div',
          { 'data-bc-grow': '' },
          h('div', { 'data-bc-label': '' }, label),
          note ? h('div', { 'data-bc-note': '' }, note) : null,
        ),
        h(Switch, { on, onChange }),
      )
    }

    /**
     * 折叠分区（2026-10-17）。
     *
     * 主人原话：「大部分配置都能够让我在设置页进行配置，
     * 同时防止一页太多，可以将相关的折叠展开进行配置」
     *
     * ★ 折叠状态**存在 React state 里**（不是 localStorage）——
     *   这样刷新页面回到默认（"常用"那几组默认展开、其余收起），
     *   不会出现"上次不小心全展开、这次一屏看不到底"。
     */
    function Collapsible({ title, subtitle, count, defaultOpen, children }) {
      const [open, setOpen] = React.useState(Boolean(defaultOpen))
      return h(
        'div',
        { 'data-bc-fold': '', 'data-open': open ? '' : undefined },
        h(
          'button',
          {
            'data-bc-foldhead': '',
            type: 'button',
            onClick: () => setOpen((v) => !v),
            'aria-expanded': open ? 'true' : 'false',
          },
          h('span', { 'data-bc-caret': '' }, '▶'),
          h('span', null, title),
          subtitle ? h('span', { 'data-bc-sub': '' }, subtitle) : null,
          h('div', { 'data-bc-grow': '' }),
          count !== undefined ? h('span', { 'data-bc-badge': '' }, `${count} 项`) : null,
        ),
        open ? h('div', { 'data-bc-foldbody': '' }, children) : null,
      )
    }

    /**
     * 通用字段编辑器（按白名单里的 `type` 渲染对应控件）。
     *
     * 值走**本地草稿**（`draft`/`setDraft`），不直接绑服务端值 ——
     * 否则 5 秒轮询会把用户正在输入的字冲掉（AGENTS.md D3）。
     */
    function Field({ spec, path, value, onDraft, onCommit }) {
      const label = h(
        'div',
        { 'data-bc-fieldlabel': '' },
        h('div', null, path),
        spec.note ? h('div', { 'data-bc-fieldnote': '' }, spec.note) : null,
      )

      if (spec.type === 'bool') {
        return h(
          'div',
          { 'data-bc-field': '' },
          label,
          h(Switch, { on: Boolean(value), onChange: (v) => onCommit(path, v) }),
        )
      }

      if (spec.type === 'choice') {
        return h(
          'div',
          { 'data-bc-field': '' },
          label,
          h(
            'select',
            {
              'data-bc-select': '',
              value: String(value ?? ''),
              onChange: (e) => onCommit(path, e.target.value),
            },
            (spec.options || []).map((opt) => h('option', { key: opt, value: opt }, opt)),
          ),
        )
      }

      const isNum = spec.type === 'int' || spec.type === 'float'
      return h(
        'div',
        { 'data-bc-field': '' },
        label,
        h('input', {
          'data-bc-input': '',
          'data-bc-num': isNum ? '' : undefined,
          type: isNum ? 'number' : 'text',
          step: spec.type === 'float' ? '0.5' : undefined,
          value: value === undefined || value === null ? '' : String(value),
          onChange: (e) => onDraft(path, e.target.value),
          onBlur: (e) => onCommit(path, e.target.value),
        }),
      )
    }

    function Panel() {
      const [st, setSt] = React.useState(null)
      const [busy, setBusy] = React.useState('')
      const [msg, setMsg] = React.useState('')
      const [tone, setTone] = React.useState('')
      const [report, setReport] = React.useState(null)
      // ⚠️ 这三个是"本地草稿"：不直接绑服务端的值
      const [kws, setKws] = React.useState(null)
      const [top, setTop] = React.useState(null)
      // ★ 通用字段的本地草稿（2026-10-17）：键是点分路径。
      //   只放"用户正在编辑"的字段；没编辑过的显示服务端值。
      const [drafts, setDrafts] = React.useState({})

      React.useEffect(() => {
        let alive = true
        const tick = async () => {
          try {
            const d = await jget('/status')
            if (alive && d?.ok) setSt(d)
          } catch {
            /* 网络抖动就下次再问 */
          }
        }
        tick()
        const timer = setInterval(tick, 5000)
        return () => {
          alive = false
          clearInterval(timer)
        }
      }, [])

      const cfg = st?.config
      React.useEffect(() => {
        if (cfg && kws === null) setKws((cfg.keywords || []).join('\n'))
        if (cfg && top === null) setTop(String(cfg.dailyTop ?? 3))
      }, [cfg, kws, top])

      const say = (text, kind) => {
        setMsg(text)
        setTone(kind || '')
      }

      const patch = async (body, what) => {
        setBusy(JSON.stringify(body))
        try {
          const r = await jpost('/config', body)
          if (r?.ok) {
            setSt((old) => ({ ...old, config: r.config, fields: r.fields || old?.fields }))
            // ★ 保存成功后**清掉对应草稿**，让界面回到服务端的权威值
            if (body && body.fields) {
              setDrafts((old) => {
                const next = { ...old }
                for (const key of Object.keys(body.fields)) delete next[key]
                return next
              })
            }
            const skipped = (r?.yaml && r.yaml.skipped) || []
            // ★ 被跳过要说出来（例如传了授权字段）—— 静默跳过最坑人
            say(
              skipped.length > 0
                ? `${what}：已保存，但有 ${skipped.length} 项被拒绝（${skipped.join('、')}）`
                : `${what}：已保存`,
              skipped.length > 0 ? 'warn' : 'good',
            )
          } else {
            say(`保存失败：${r?.error || '未知原因'}`, 'warn')
          }
        } catch (error) {
          say(String(error?.message ?? error), 'warn')
        } finally {
          setBusy('')
        }
      }

      /**
       * 通用字段提交（2026-10-17）。
       *
       * 值先过**类型转换**（界面上的 input 永远是字符串），
       * 再走 `/config` 的 `fields` 通道写进 `config.yaml`。
       */
      const commitField = (path, rawValue, spec) => {
        let value = rawValue
        if (spec && (spec.type === 'int' || spec.type === 'float')) {
          const n = Number(rawValue)
          if (!Number.isFinite(n)) {
            say(`${path}：不是合法数字`, 'warn')
            return
          }
          value = spec.type === 'int' ? Math.trunc(n) : n
        }
        patch({ fields: { [path]: value } }, path)
      }

      const onRun = async () => {
        setBusy('run')
        say('')
        try {
          const r = await jpost('/run', {})
          say(
            r?.started
              ? `已经在后台跑了（pid ${r.pid}）—— 一轮大约 3 分钟，完了点「看今天的日报」`
              : r?.hint || '没起来',
            r?.started ? 'good' : 'warn',
          )
        } catch (error) {
          say(String(error?.message ?? error), 'warn')
        } finally {
          setBusy('')
        }
      }

      const onShowReport = async () => {
        if (report !== null) {
          setReport(null)
          return
        }
        setBusy('report')
        try {
          const r = await jget('/report')
          setReport(r?.exists ? r.text || '（日报是空的）' : '今天还没有日报 —— 点「现在跑一轮」')
        } catch (error) {
          setReport(`读不到日报：${String(error?.message ?? error)}`)
        } finally {
          setBusy('')
        }
      }

      if (!cfg) {
        return h('div', { 'data-bc-page': '' }, h('div', { 'data-bc-note': '' }, '读设置中…'))
      }

      // ★ 白名单元数据与当前值（后端给，前端不另维护字段表）
      const schema = st.schema || {}
      const fields = st.fields || {}
      const neverEditable = st.neverEditable || []

      return h(
        'div',
        { 'data-bc-page': '' },

        /* ── 今天 ── */
        h(
          'div',
          { 'data-bc-row': '' },
          h('span', { 'data-bc-badge': '' }, st.pythonFound ? '环境已就绪' : '环境未装'),
          h('span', { 'data-bc-badge': '' }, st.reportExists ? '今天已有日报' : '今天还没跑'),
          h('span', { 'data-bc-badge': '' }, `每天最多挑 ${cfg.dailyTop} 条`),
          h('div', { 'data-bc-grow': '' }),
          h('button', { 'data-bc-btn': '', 'data-primary': '', disabled: busy === 'run', onClick: onRun }, '现在跑一轮'),
        ),
        msg
          ? h(
              'div',
              {
                'data-bc-msg': '',
                'data-good': tone === 'good' ? '' : undefined,
                'data-warn': tone === 'warn' ? '' : undefined,
              },
              msg,
            )
          : null,

        /* ── 关注什么 ── */
        h(
          'div',
          { 'data-bc-sec': '' },
          h('div', { 'data-bc-h': '' }, '关注什么'),
          h('textarea', {
            'data-bc-input': '',
            rows: 5,
            placeholder: '一行一个关键词；留空就用 Python 那份配置里的关键词',
            value: kws ?? '',
            onChange: (e) => setKws(e.target.value),
          }),
          h(
            'div',
            { 'data-bc-row': '' },
            h('div', { 'data-bc-grow': '' }, h('div', { 'data-bc-label': '' }, '每天最多挑几条')),
            h('input', {
              'data-bc-input': '',
              'data-bc-num': '',
              type: 'number',
              min: 0,
              max: 20,
              value: top ?? '',
              onChange: (e) => setTop(e.target.value),
              onBlur: () => {
                const n = Math.max(0, Math.min(Number(top) || 0, 20))
                setTop(String(n))
                patch({ dailyTop: n }, '每天挑几条')
              },
            }),
          ),
          h(
            'div',
            { 'data-bc-row': '' },
            h(
              'button',
              {
                'data-bc-btn': '',
                disabled: Boolean(busy),
                onClick: () =>
                  patch(
                    {
                      keywords: String(kws || '')
                        .split('\n')
                        .map((s) => s.trim())
                        .filter(Boolean),
                    },
                    '关键词',
                  ),
              },
              '保存',
            ),
            h('div', { 'data-bc-note': '' }, '关键词改了之后，「现在跑一轮」就按它去搜'),
          ),
        ),

        /* ── ★ 高级设置（折叠分区，2026-10-17）─────────────────────────
           主人要求：「大部分配置都能够让我在设置页进行配置，
           同时防止一页太多，可以将相关的折叠展开进行配置」
           ⇒ 按主题分组、默认只展开最常用的两组。 */
        ...FIELD_GROUPS.map((group) => {
          const paths = group.paths.filter((p) => schema[p] !== undefined)
          if (paths.length === 0) return null
          return h(
            Collapsible,
            {
              key: group.title,
              title: group.title,
              subtitle: group.subtitle,
              count: paths.length,
              defaultOpen: Boolean(group.defaultOpen),
            },
            ...paths.map((path) => {
              const spec = schema[path]
              const serverValue =
                fields[path] !== undefined ? fields[path] : undefined
              const shown =
                drafts[path] !== undefined ? drafts[path] : serverValue
              return h(Field, {
                key: path,
                spec,
                path,
                value: shown,
                onDraft: (p, v) => setDrafts((old) => ({ ...old, [p]: v })),
                onCommit: (p, v) => commitField(p, v, spec),
              })
            }),
          )
        }),

        /* ── 不能在这里改的（明确告知，避免"找不到"） ── */
        h(
          Collapsible,
          {
            title: '只能去终端改的（授权类）',
            subtitle: `${neverEditable.length} 项`,
            defaultOpen: false,
          },
          h(
            'div',
            { 'data-bc-note': '' },
            '下面这些**改的是授权或安全状态**，界面上点一下就生效等于绕过了"人审"这道闸门，',
            '所以刻意不放按钮 —— 请在终端跑 ',
            h('code', null, './.venv/Scripts/python.exe -m cli.app config'),
            '：',
          ),
          ...neverEditable.map((path) =>
            h('div', { key: path, 'data-bc-fieldnote': '' }, `· ${path}`),
          ),
        ),

        /* ── 日报 ── */
        h(
          'div',
          { 'data-bc-sec': '' },
          h('div', { 'data-bc-h': '' }, '它挑出来的东西'),
          h(
            'div',
            { 'data-bc-row': '' },
            h('button', { 'data-bc-btn': '', disabled: busy === 'report', onClick: onShowReport }, report !== null ? '收起日报' : '看今天的日报'),
          ),
          report !== null ? h('pre', { 'data-bc-pre': '' }, report) : null,
        ),

        /* ── 它是怎么干活的（给他看的） ── */
        h(
          'div',
          { 'data-bc-sec': '' },
          h('div', { 'data-bc-h': '' }, '它的地盘'),
          h(
            'div',
            { 'data-bc-kv': '' },
            `项目目录：${st.projectDir}`,
            h('br'),
            `插件家目录：${st.home}`,
            h('br'),
            `今天的日报：${st.reportPath}`,
          ),
          h(
            'div',
            { 'data-bc-note': '' },
            '★ 发布授权（能不能真发评论/点赞）不在这里改 —— ',
            '那是"改授权"，仍走终端 ',
            h('code', null, './.venv/Scripts/python.exe -m cli.app config'),
            '；本页只负责挑内容与看结果。',
          ),
        ),
      )
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        // ★★ 失败软着陆（来自 dsh-market issue #869 的教训）：
        //    浏览器端一个插件抛异常，DSH 桌面端是**整个 shell 起不来**，
        //    而不是"只有这个插件不出现"。所以这里整体包一层 try/catch ——
        //    最坏结果是本插件设置页不出现 + 控制台留一行原因，
        //    绝不把用户的其他插件一起拖下水。
        try {
          applyInner(ctx)
        } catch (error) {
          try {
            console.error('[bili-companion] 设置页挂载失败（插件本身已跳过）', error)
          } catch {
            /* 控制台都不可用就只能放弃 */
          }
        }
      },
    }

    function applyInner(ctx) {
      const style = document.createElement('style')
      style.setAttribute('data-plugin-css', 'dsh-bili-companion')
      style.textContent = CSS
      document.head.appendChild(style)
      ctx.effect(() => () => style.remove(), 'bili-companion: 设置页样式')

      // 兼容两种形态：新版直接挂在 ctx.slots 上，旧版要 ctx.get('slots')
      const slots =
        (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots
      if (!slots) return
      slots.inject('settings.section', () =>
        slots.register(
          { name: 'settings.section', id: 'bili-companion', order: 30, label: 'B站小号' },
          () => h(Panel),
        ),
      )
    }
  },
})
