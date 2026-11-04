export function lineSchedule(line) {
  return line.screen_calendar || (line.interval_minutes ? { mode: 'interval', minutes: line.interval_minutes } : { mode: 'off' })
}

export const weekdayNames = ['一', '二', '三', '四', '五', '六', '日']

export function scheduleLabel(plan) {
  if (!plan || plan.mode === 'off') return ''
  if (plan.mode === 'interval') return `每 ${plan.minutes} 分钟`
  const days = plan.mode === 'weekly' ? '周' + plan.weekdays.map(d => weekdayNames[d - 1]).join('、') : '每天'
  return days + ' ' + plan.times.join('、')
}

export function nextScreenLabel(epoch) {
  if (!epoch) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
  }).format(new Date(epoch * 1000))
}
