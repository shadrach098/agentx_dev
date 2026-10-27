"""Notes kept in memory, by id."""

from notes.text import first_line, slugify


class NoteStore:
    def __init__(self):
        self.notes = {}

    def add(self, text):
        base = slugify(first_line(text))
        note_id, number = base, 2
        while note_id in self.notes:
            note_id = f"{base}-{number}"
            number += 1
        self.notes[note_id] = text
        return note_id

    def get(self, note_id):
        return self.notes[note_id]

    def titles(self):
        return {note_id: first_line(text) for note_id, text in self.notes.items()}
