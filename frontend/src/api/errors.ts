const GENERIC_MESSAGE = 'Something went wrong';

/**
 * Turn an API error `detail` into a displayable string.
 * FastAPI returns a string for HTTPException and an array of
 * {loc, msg, type} objects for 422 validation errors.
 */
export function getErrorMessage(detail: unknown): string {
    if (typeof detail === 'string') return detail;

    if (Array.isArray(detail)) {
        const parts = detail
            .map((item) => {
                if (typeof item === 'string') return item;
                if (!item || typeof item !== 'object' || typeof item.msg !== 'string') return '';
                const loc: unknown[] = Array.isArray(item.loc) ? item.loc : [];
                // loc looks like ["body", "email"]; use the last string segment as the field name
                const field = [...loc].reverse().find((s) => typeof s === 'string' && s !== 'body');
                return field ? `${field}: ${item.msg}` : item.msg;
            })
            .filter(Boolean);
        if (parts.length > 0) return parts.join('; ');
    }

    return GENERIC_MESSAGE;
}
