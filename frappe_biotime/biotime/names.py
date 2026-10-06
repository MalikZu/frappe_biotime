"""Compare people's names as HR reads them."""

import re
import unicodedata

#: Arabic letters written more than one way: teh marbuta as heh, alef maksura as yeh, and
#: no tatweel. Hamza forms fold through NFKD.
_ARABIC = str.maketrans(
	{
		"\N{ARABIC LETTER TEH MARBUTA}": "\N{ARABIC LETTER HEH}",
		"\N{ARABIC LETTER ALEF MAKSURA}": "\N{ARABIC LETTER YEH}",
		"\N{ARABIC TATWEEL}": "",
	}
)


def words(name: str | None) -> frozenset[str]:
	"""The words of a name, compared without case, accents or Arabic spelling variants."""
	text = unicodedata.normalize("NFKD", name or "")
	text = "".join(char for char in text if not unicodedata.combining(char))
	return frozenset(re.findall(r"\w+", text.casefold().translate(_ARABIC)))
