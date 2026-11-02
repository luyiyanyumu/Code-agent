// Resource errors are distinct from an unavailable route, permissions or a
// failed disk read. Older backends reported a missing file as a specific 422.
export function isMissingProjectFile(error) {
  if (error?.code === 'PROJECT_FILE_NOT_FOUND') return true
  const message = String(error?.message || '')
  if (![404, 422].includes(error?.status)) return false
  return /(?:^|：|:)\s*文件不存在\s*[:：]|\bno such file\b|\bfile (?:is )?(?:missing|gone|not found|does not exist)\b/i.test(message)
}
