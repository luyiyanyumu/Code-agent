/** Keep historical general sessions, including records from before project_id existed. */
export function generalChatSessions(sessions) {
  return Array.isArray(sessions) ? sessions.filter(session => session && !session.project_id) : []
}
