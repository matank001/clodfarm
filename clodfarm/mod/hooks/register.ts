import type { EngineInterface, Register } from 'claude-code'

// The farm's messaging from inside Claude Code. The farm keeps every message in its store and touches this session's
// flag file (FARM_MAIL_FLAG) when one waits for its Claude or sub-agent; the settings hooks it installs
// (auth.farm_hooks) hand the mail over at a prompt, after a batch of tool calls and before a turn ends. This module
// does the parts a shell hook can't do well:
//   * wakes an idle conversation when its mail arrives, with a prompt of its own, instead of a `clodfarm hook --listen`
//     process left polling for ten minutes after every turn (and Claude Code waiting up to 30 s for it at exit)
//   * gates and logs every message Claude Code's SendMessage sends (the main loop, its agents, other plugins) at
//     session.send, where a refusal reads as the tool's own and only what was delivered is logged
//   * gives the model `msg`, the farm's own messages as a tool rather than a shell command
// It sets FARM_MOD_LIVE for everything the session starts, so the settings hooks' shell versions of those steps stand
// down; a session that didn't load it keeps their behaviour.

const POLL_MS = 1000
const LISTEN_MS = 600_000 // auth.LISTEN_SECONDS: a conversation is woken for mail this long after its last turn
const MSG = 'msg'

type Reply = { text?: string; ok?: boolean; why?: string }
type MsgInput = { to?: unknown; text?: unknown; urgent?: unknown; wake?: unknown }

/** `clodfarm hook --mod <action>`: the input as JSON on stdin, a JSON answer on stdout (see cli._mod). */
async function farm($: EngineInterface, action: string, input: Record<string, unknown> = {}): Promise<Reply> {
  const run = await $.process.run(['clodfarm', 'hook', '--mod', action],
    { stdin: JSON.stringify(input), timeoutMs: 20_000 })
  try {
    return JSON.parse(run.stdout || '{}') as Reply
  } catch {
    return {}
  }
}

/** This session's farm state: module values, so a reload starts them over (the mail waits in the store meanwhile). */
const my = {
  flag: undefined as string | undefined, // set only in a farm session: every hook below stands aside otherwise
  busy: false, // the main loop's turn is running: the settings hooks deliver mail inside it
  lastTurnEnd: undefined as number | undefined,
  waking: false,
}

/** Every POLL_MS in a conversation: when its mail flag is up and its last turn ended a little while ago, take the
 * mail and start a turn with it. */
async function wake($: EngineInterface) {
  if (!my.flag || my.busy || my.waking || my.lastTurnEnd === undefined) return
  if ((await $.clock.now()) - my.lastTurnEnd > LISTEN_MS) return // its mail waits for its next prompt
  if (!(await $.fs.exists(my.flag))) return
  my.waking = true
  try {
    const { text } = await farm($, 'take', { how: 'wake' })
    if (text) {
      my.busy = true // until that turn ends, so the next tick doesn't take more for it
      await $.prompt.submit({ text })
    }
  } finally {
    my.waking = false
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const task = await $.env.get('FARM_TASK_ID')
    my.flag = task === 'usage' ? undefined : await $.env.get('FARM_MAIL_FLAG')
    if (!my.flag) return next(e)
    await $.env.set('FARM_MOD_LIVE', '1')
    await $.tool.register({
      name: MSG,
      description:
        'Send a message to another Claude on this farm (by its name) or to a sub-agent (by its id), the same as ' +
        '`clodfarm msg`. A Claude\'s conversations get it at their next tool call or prompt, and one active in the ' +
        'last 10 minutes is woken for it; a running sub-agent gets it at its next tool call. `urgent` interrupts a ' +
        'running sub-agent now; `wake` starts someone to handle it if nobody has read it in a while.',
      inputSchema: {
        type: 'object',
        properties: {
          to: { type: 'string', description: 'A Claude\'s name, or a sub-agent\'s id' },
          text: { type: 'string', description: 'The message' },
          urgent: { type: 'boolean', description: 'Interrupt that running sub-agent with it now' },
          wake: { type: 'boolean', description: 'If nobody reads it in time, start someone to handle it' },
        },
        required: ['to', 'text'],
      },
    })
    if (!task) $.clock.every(POLL_MS, () => void wake($)) // a conversation: a sub-agent's run ends with its turn
    return next(e)
  })

  on('turn.start', ($, e, next) => {
    my.busy = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      my.busy = false
      my.lastTurnEnd = await $.clock.now()
    }
    return next(e)
  })

  // A message to a Claude whose person approves everything sent to it goes through `clodfarm msg`, which asks them;
  // the farm's own errors never stop a message
  on('session.send', async ($, e, next) => {
    if (!my.flag) return next(e)
    const gate = await farm($, 'send', { to: e.to }).catch((): Reply => ({}))
    if (gate.ok === false) return { isDelivered: false, reason: gate.why || 'the farm turned it away' }
    const sent = await next(e)
    if (sent.isDelivered) await farm($, 'sent', { to: e.to, text: e.text }).catch(() => undefined)
    return sent
  })
    .catch(($, e, next) => next(e))

  on('tool.call', { tool: 'mcp__clodfarm__msg' }, async ($, e) => {
    const { to, text, urgent, wake } = e as MsgInput
    if (typeof to !== 'string' || typeof text !== 'string') return { deny: 'msg takes `to` and `text`' }
    const run = await $.process.run(
      ['clodfarm', 'msg', ...(urgent === true ? ['--urgent'] : []), ...(wake === true ? ['--wake'] : []), '--', to, '-'],
      { stdin: text, timeoutMs: 30_000 })
    const out = (run.stdout.trim() || run.stderr.trim())
    return run.exitCode === 0 ? { result: out } : { deny: out || `clodfarm msg exited ${run.exitCode}` }
  })
}
