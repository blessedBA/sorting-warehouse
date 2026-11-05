"""Проверки протокола быстрого веса и ручной консоли без подключения платы."""

import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import controller
from hardware import Hardware
from main import measureWeight


class SerialStub:
    def __init__(self):
        self.is_open = True
        self.buffer = bytearray(b"READY\n")
        self.commands = []
        self.replies = {
            b"WEIGHT?\n": b"WEIGHT 12.345\n",
            b"WEIGHT_FAST?\n": b"WEIGHT -0.125\n",
            b"STOP\n": b"STOPPED\n",
        }

    @property
    def in_waiting(self):
        return len(self.buffer)

    def read(self, count):
        # Ответ может приходить частями, как в настоящем serial.
        data = self.buffer[:min(count, 3)]
        del self.buffer[:len(data)]
        return bytes(data)

    def write(self, data):
        self.commands.append(data)
        self.buffer.extend(self.replies[data])
        return len(data)


class FastWeightTests(unittest.TestCase):
    def test_sorter_averages_and_publishes_fast_readings_until_deadline(self):
        hardware = Mock()
        hardware.get_weight.side_effect = [2.0, 4.0, 9.0]
        readings = []
        # Последний замер завершается после границы окна и всё равно учитывается.
        with patch("main.monotonic", side_effect=[0.0, 0.3, 0.6, 1.1]):
            weight = measureWeight(hardware, 1.0, on_weight=readings.append)
        self.assertEqual(weight, 5.0)
        self.assertEqual(readings, [2.0, 4.0, 9.0])
        self.assertEqual(hardware.get_weight.call_count, 3)
        for request in hardware.get_weight.call_args_list:
            self.assertEqual(request.args, ())
            self.assertEqual(request.kwargs, {"fast": True})

    def test_normal_and_fast_requests_on_same_connection(self):
        port = SerialStub()
        with patch("hardware.serial.Serial", return_value=port):
            hardware = Hardware()
        self.assertEqual(hardware.get_weight(), 12.345)
        self.assertEqual(hardware.get_weight(fast=True), -0.125)
        self.assertEqual(hardware.get_weight(), 12.345)
        self.assertEqual(port.commands, [b"WEIGHT?\n", b"WEIGHT_FAST?\n", b"WEIGHT?\n"])

    def test_old_firmware_error_and_stop_recovery(self):
        port = SerialStub()
        port.replies[b"WEIGHT_FAST?\n"] = b"UNKNOWN\n"
        with patch("hardware.serial.Serial", return_value=port):
            hardware = Hardware()
        with self.assertRaisesRegex(RuntimeError, "UNKNOWN"):
            hardware.get_weight(fast=True)
        hardware.stop()
        self.assertEqual(hardware.get_weight(), 12.345)

    def test_continuous_interrupt_cancels_measurement(self):
        hardware = Mock()
        hardware.get_weight.side_effect = [-0.125, KeyboardInterrupt()]
        output = io.StringIO()
        with redirect_stdout(output):
            controller.showWeight(hardware, continuous=True, fast=True)
        self.assertIn("Масса: -0.125 г", output.getvalue())
        hardware.stop.assert_called_once_with()
        self.assertEqual(hardware.get_weight.call_count, 2)
        hardware.get_weight.assert_called_with(fast=True)

    def run_console(self, command, *, sorting=False):
        hardware = Mock()
        hardware.zero.return_value = []
        session = Mock()
        session.isRunning.return_value = sorting
        output = io.StringIO()
        with patch("sys.argv", ["controller.py", "--no-log-window"]), \
                patch("builtins.input", side_effect=[command, "quit"]), \
                patch.object(controller, "Hardware", return_value=hardware), \
                patch.object(controller, "SortingSession", return_value=session), \
                patch.object(controller, "SorterLog"), \
                patch.object(controller, "shutdownHardware"), \
                patch.object(controller, "showWeight") as show_weight, \
                patch.object(controller, "showSortingWeight") as show_sorting, \
                redirect_stdout(output):
            controller.main()
        return hardware, show_weight, show_sorting, output.getvalue()

    def test_console_modes(self):
        for command, fast, continuous in [
            ("weight", False, False),
            ("weight continuous", False, True),
            ("weight fast", True, False),
            ("weight fast continuous", True, True),
            ("weight continuous FAST", True, True),
        ]:
            with self.subTest(command=command):
                hardware, show_weight, _, _ = self.run_console(command)
                show_weight.assert_called_once_with(hardware, continuous=continuous, fast=fast)

    def testFastModeShowsSorterReadingsWithoutAccessingHardware(self):
        for command, continuous in [
            ("weight fast", False),
            ("weight fast continuous", True),
            ("weight continuous FAST", True),
        ]:
            with self.subTest(command=command):
                hardware, show_weight, show_sorting, output = self.run_console(command, sorting=True)
                show_weight.assert_not_called()
                show_sorting.assert_called_once_with(unittest.mock.ANY, continuous=continuous)
                hardware.get_weight.assert_not_called()
                self.assertNotIn("Быстрый вес доступен после main stop", output)

    def test_normal_view_during_sorting_is_passive(self):
        _, show_weight, show_sorting, _ = self.run_console("weight continuous", sorting=True)
        show_weight.assert_not_called()
        self.assertTrue(show_sorting.call_args.kwargs["continuous"])

    def test_invalid_options_do_not_start_measurements(self):
        for command in ("weight fast fast", "weight continuous continuous", "weight faster"):
            with self.subTest(command=command):
                _, show_weight, show_sorting, output = self.run_console(command)
                show_weight.assert_not_called()
                show_sorting.assert_not_called()
                self.assertIn("Использование: weight [fast] [continuous]", output)


if __name__ == "__main__":
    unittest.main()
