import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

// The farm beneath the mod: `clodfarm hook --mod` and `clodfarm msg` answered from memory, the flag file a boolean
function farm(on: On, mail: { flag: boolean; text: string; approving?: boolean }) {
  // the session beneath: what the engine would answer
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('env.set', () => ({ value: undefined }))
  on('tool.register', ($, e) => ({ value: { tool: `mcp__clodfarm__${e.name}` } }) as never)
  on('turn.start', ($, e) => ({ turnId: e.turnId }) as never)
  on('turn.complete', () => ({ text: '' }))
  const calls: { argv: readonly string[]; stdin: unknown }[] = []
  on('fs.exists', () => ({ value: mail.flag }))
  on('process.run', ($, e) => {
    const raw = e.init?.stdin
    const stdin = e.argv[1] === 'msg' ? raw : raw ? JSON.parse(raw) : undefined
    calls.push({ argv: e.argv, stdin })
    const action = e.argv[3]
    let out: unknown = {}
    if (e.argv[1] === 'msg') out = 'sent m1 to gil'
    else if (action === 'take') {
      out = { text: mail.flag ? mail.text : '' }
      mail.flag = false
    } else if (action === 'send') out = mail.approving ? { ok: false, why: 'gil approves' } : { ok: true, why: '' }
    else if (action === 'sent') out = { ok: true }
    return { value: { exitCode: 0, stdout: typeof out === 'string' ? out : JSON.stringify(out), stderr: '',
                      isStdoutTruncated: false, isStderrTruncated: false } } as never
  })
  return calls
}

test('an idle conversation is woken for its mail, and only for a while after its last turn', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { FARM_MAIL_FLAG: '/mail/test' })
  const mail = { flag: false, text: '[farm message m1 from gil] are you around?' }
  const calls = farm(on, mail)
  const prompts: string[] = []
  on('prompt.submit', ($, e) => {
    prompts.push(e.text)
    return { text: e.text } as never
  })
  await $.session.start({ cwd: '/workspace/repo', surface: null, isInteractive: false })

  mail.flag = true
  await clock.advance(5_000)
  expect(prompts).toEqual([]) // no turn yet: nothing to wake

  await $.turn.start({ text: 'hi', turnId: 't1' })
  await clock.advance(5_000)
  expect(prompts).toEqual([]) // mid-turn the settings hooks deliver it
  await $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' } as never)
  await clock.advance(1_500)
  expect(prompts).toEqual([mail.text])
  expect(calls.filter(c => c.argv[3] === 'take').length).toBe(1)

  await $.turn.complete({ answer: 'ok', durationMs: 1, isAborted: false, turnId: 't2', reason: 'answer' } as never)
  await clock.advance(700_000)
  mail.flag = true
  await clock.advance(5_000)
  expect(prompts.length).toBe(1) // ten minutes on: it waits for the next prompt
})

test('SendMessage to a Claude whose person approves everything is turned away; a delivered one is logged',
  async ($, on) => {
    mock.clock(on)
    mock.env(on, { FARM_MAIL_FLAG: '/mail/test' })
    const mail = { flag: false, text: '', approving: true }
    const calls = farm(on, mail)
    on('session.send', () => ({ isDelivered: true }))
    await $.session.start({ cwd: '/workspace/repo', surface: null, isInteractive: false })

    const refused = await $.session.send({ to: '[clodfarm] farm · gil', text: 'take the tests', origin: { kind: 'model' } })
    expect(refused).toEqual({ isDelivered: false, reason: 'gil approves' })
    mail.approving = false
    const sent = await $.session.send({ to: '[clodfarm] farm · gil', text: 'take the tests', origin: { kind: 'model' } })
    expect(sent.isDelivered).toBe(true)
    expect(calls.filter(c => c.argv[3] === 'sent').map(c => c.stdin))
      .toEqual([{ to: '[clodfarm] farm · gil', text: 'take the tests' }])
  })

test('outside the farm the mod stands aside', async ($, on) => {
  mock.env(on, {})
  const calls = farm(on, { flag: true, text: 'x', approving: true })
  on('session.send', () => ({ isDelivered: true }))
  await $.session.start({ cwd: '/tmp', surface: null, isInteractive: true })
  const sent = await $.session.send({ to: 'gil', text: 'hi', origin: { kind: 'model' } })
  expect(sent.isDelivered).toBe(true)
  expect(calls).toEqual([])
})

test('the msg tool is clodfarm msg', async ($, on) => {
  mock.env(on, { FARM_MAIL_FLAG: '/mail/test', FARM_TASK_ID: '261006120000abcdef' })
  const calls = farm(on, { flag: false, text: '' })
  await $.session.start({ cwd: '/workspace/repo', surface: null, isInteractive: false })
  const r = await $.tool.call({ tool: 'mcp__clodfarm__msg', to: 'gil', text: 'rebased', urgent: true } as never)
  expect(r.result).toBe('sent m1 to gil')
  expect(calls[0]).toEqual({ argv: ['clodfarm', 'msg', '--urgent', '--', 'gil', '-'], stdin: 'rebased' })
})
