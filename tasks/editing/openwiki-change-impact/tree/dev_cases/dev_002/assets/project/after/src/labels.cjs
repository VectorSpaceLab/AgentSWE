function label(name, options = {}) {
  const separator = options.separator ?? " ->";
  return `${name}${separator} Admin`;
}

module.exports = { label };
