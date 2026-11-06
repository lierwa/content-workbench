// A verification pass only admits a story to the editorial comparison.
// Keep it distinct from the choice that actually goes to production.
export function draftRound(s, e) {
  return s.draft_rounds?.find(r => r.method === 'ai' && r.batch_id === e.batch_id)
}
export function verdict(s, e) {
  return draftRound(s, e)?.choices?.find(c => c.id === e.id && c.version === e.version)
}
export function allTopics(s) {
  const events = s.review?.events || []
  const covered = new Set(events.flatMap(e => e.refs))
  return [...events, ...s.items.filter(i => !covered.has(i.ref)).map(i => ({id:i.ref, refs:[i.ref]}))]
}
export function pickedTopics(s) {
  return (s.review?.events || []).filter(e => {
    const c = verdict(s, e)
    return !e.production_excluded && e.comparison?.selected && c && ['二创','搬运','入选'].includes(c.use)
      && draftRound(s, e).ids.includes(e.id)
  }).sort((a,b) => verdict(s,a).priority - verdict(s,b).priority)
}
const stageNames = {initial:'初筛', selection:'二筛取材', verification:'三筛核实'}
const resultNames = {initial:{advance:'留作比较',drop:'初筛淘汰'}, selection:{advance:'列入取材',deferred:'本轮未取材',drop:'未选取材'},
  verification:{advance:'正文已核实',drop:'核实未通过'}}
export function screeningLabel(r) {
  return r.invalid_model_output || r.model_unavailable ? '判断待核' : resultNames[r.stage]?.[r.decision] ||
    ({insufficient:'材料不足',deferred:'暂缓',drop:'未通过',advance:'通过'})[r.decision] || '待判断'
}
export const stageName = r => stageNames[r.stage] || r.stage
export function judgment(s,e) {
  const c = verdict(s,e)
  if (e.production_excluded && c && ['二创','搬运','入选'].includes(c.use))
    return {label:'已撤下', note:'制作时已撤下此题。', tone:'hold'}
  if (c && (e.comparison?.selected || c.use==='不选' || c.invalid_model_output)) {
    if (c.invalid_model_output) return {label:'选稿待核',note:c.reason || '未取得有效的选稿答复。',tone:'hold'}
    const picked = ['二创','搬运','入选'].includes(c.use)
    return {label:picked?(c.use==='入选'?'本期入选':c.use+'入选'):c.use==='不选'?'本期不选':c.use, basis:c.basis, rank:c.priority,
      note:c.reason, angle:picked ? (e.assessment?.round?.dimensions?.creative_value?.judgment || e.assessment?.round?.angle) : null,
      tone:picked?'pick':c.use==='候补'?'hold':'neutral'}
  }
  if (e.comparison?.selected && (s.status==='stopped' || draftRound(s,e)?.status==='cancelled'))
    return {label:'选稿未完成',note:'本轮已停止，尚无本版本的选稿结论。',tone:'hold'}
  if (e.comparison?.selected) return {label:'待选稿',note:'正文已核实，尚无本版本的选稿结论。',tone:'hold'}
  if (e.comparison?.selected===false) return {label:'需复核',note:e.comparison.reason,tone:'hold'}
  if (e.error || e.invalid_model_output || e.model_unavailable) return {label:'待复核',note:'这条的筛选尚未完成。',tone:'hold'}
  const steps=e.screening?.rounds || [], r=steps.at(-1)
  return r ? {label:screeningLabel(r),note:r.reason,tone:'neutral'}
    : {label:'待筛选',note:'还没有筛选记录。',tone:'neutral'}
}

const phases = ['initial', 'selection', 'verification']
export function screeningPath(s, e) {
  const rounds = e.screening?.rounds || []
  const steps = []
  for (const stage of phases) {
    const row = rounds.findLast(r => r.stage === stage)
    if (!row) {
      if (rounds.some(r=>phases.indexOf(r.stage)>phases.indexOf(stage))) {
        steps.push({stage, label:'无记录', decision:'unknown', note:'没有保存这一轮的判断。'})
        continue
      }
      const stopped = ['stopped','ended'].includes(s.status)
      const interrupted = stopped || ['partial','failed'].includes(s.status)
      const issue = e.error || Object.values(e.source_errors || {}).find(Boolean)
      steps.push({stage, label:issue?'未能完成':interrupted?'未完成':'待处理', decision:'pending',
        note:issue || (stopped?'本轮已停止。':interrupted?'本轮未完成这一环节。':'尚无本轮结论。')})
      break
    }
    const invalid = row.invalid_model_output || row.model_unavailable || (stage==='initial' && e.initial?.some(i=>i.invalid_model_output))
    steps.push({stage, label:invalid?'判断待核':screeningLabel(row), decision:invalid?'pending':row.decision,
      rank:row.priority, note:row.reason || '本轮没有记录理由。'})
    if (steps.at(-1).decision !== 'advance') break
  }
  return steps
}
// Categories are disjoint: a deferred or interrupted story never counts as a rejection.
export function topicOutcome(s, e, steps=screeningPath(s,e)) {
  const last = steps.at(-1), c = verdict(s,e)
  if (last.decision==='pending') return 'pending'
  if (last.decision!=='advance') return last.stage
  if (e.production_excluded && c && ['二创','搬运','入选'].includes(c.use)) return 'removed'
  if (!e.comparison?.selected || !c || c.invalid_model_output) return 'pending'
  if (c.use==='候补') return 'reserve'
  if (c.use==='不选') return 'passed'
  return draftRound(s,e)?.ids?.includes(e.id) && ['二创','搬运','入选'].includes(c.use) ? 'picked':'pending'
}
export const screeningTabs = [
  {id:'initial', label:'一筛'}, {id:'selection', label:'二筛'}, {id:'verification', label:'三筛'}
]

function tabDecision(s,e,step,steps) {
  if (step.stage==='verification' && step.decision==='advance') {
    const outcome = topicOutcome(s,e,steps)
    const final = judgment(s,e)
    if (outcome==='picked') return {...final,label:'本期入选',order:0,outcome}
    if (outcome==='reserve' || outcome==='passed') {
      return {...final,label:'核实后未选',tone:'neutral',order:1,outcome:'passed'}
    }
    return {...final,order:3,outcome}
  }
  const advance=step.decision==='advance'
  const pending=['pending','unknown'].includes(step.decision)
  return {label:step.label,note:step.note,rank:step.rank,
    order:advance?0:pending?3:2, tone:advance?'pick':pending?'hold':'neutral', outcome:step.decision}
}

function listReason(text, outcome) {
  let value = String(text || '').trim()
  if (!value || ['尚无本轮结论。','本轮没有记录理由。'].includes(value)) return ''
  if (outcome!=='picked' && outcome!=='advance') {
    // Often the first clause praises the story and the contrast explains why it stopped.
    const contrast = value.match(/[，；。]\s*(?:但(?:是)?|不过)\s*(.+)$/u)
    if (contrast) value = contrast[1]
  }
  const limit = 24
  if (value.length<=limit) return value
  const clauses = value.split(/[。！？；，]/u).map(part=>part.trim()).filter(Boolean)
  const fits = part=>part.length>=5 && part.length<=limit && !/^(如果|虽然|尽管|由于|除非|以及)/u.test(part)
  const short = clauses.filter(fits)
  if (outcome==='picked') {
    const angle = short.find(part=>/^(讲清|回答|说明|解释|提醒|帮助读者|可核对)/u.test(part))
    if (angle) return angle
  }
  if (short.length && short[0]===clauses[0]) {
    if (outcome==='passed' && /名额|仅选|只选/u.test(short[0]) && fits(clauses[1] || '')) return clauses[1]
    return short[0]
  }
  return value.slice(0,limit-1)+'…'
}

export function topicsByStage(s) {
  const groups = Object.fromEntries(screeningTabs.map(tab=>[tab.id,[]]))
  for (const e of allTopics(s)) {
    const steps = screeningPath(s,e)
    for (const step of steps) {
      const decision = tabDecision(s,e,step,steps)
      groups[step.stage].push({e,steps,decision,shortReason:listReason(decision.note,decision.outcome)})
    }
  }
  for (const rows of Object.values(groups)) {
    rows.sort((a,b)=>a.decision.order-b.decision.order
      || (a.decision.rank ?? Infinity)-(b.decision.rank ?? Infinity))
  }
  return groups
}
