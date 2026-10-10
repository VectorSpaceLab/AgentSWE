export const getRecord = (id) => `record:${id}`;

/** @deprecated Use getRecord. */
export const fetchRecord = (id) => getRecord(id);
