class ClassTime:
    def __init__(self, start_time, stop_time, duration_min, slot_label, evening, days_allowed):
        """
        Represents an allowed class time slot.

        :param start_time: Start time of the class (string, e.g. '08:00:00')
        :param stop_time: End time of the class (string, e.g. '09:15:00')
        :param duration_min: Duration in minutes (int)
        :param slot_label: Label for the slot (e.g. 'lecture_75', 'lab_~110')
        :param evening: Boolean indicating if it's an evening slot
        :param days_allowed: List of allowed days (e.g. ['M', 'T', 'W', 'Th', 'F'])
        """
        self.start_time = start_time
        self.stop_time = stop_time
        self.duration_min = duration_min
        self.slot_label = slot_label
        self.evening = evening
        self.days_allowed = days_allowed

    def __repr__(self):
        return f"<{self.slot_label}: {self.start_time}-{self.stop_time}, {self.duration_min} min, Days={','.join(self.days_allowed)}, Evening={self.evening}>"
