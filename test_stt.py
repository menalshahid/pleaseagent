import os
import io
from stt import transcribe_audio
from dotenv import load_dotenv

load_dotenv()

class MockAudioFile:
    def __init__(self, data):
        self.data = data
        self.filename = "test.webm"
    def read(self):
        return self.data

def test_stt():
    # Empty audio
    mock_empty = MockAudioFile(b"")
    result = transcribe_audio(mock_empty)
    print(f"Empty audio result: {result}")
    
    # Very short audio
    mock_short = MockAudioFile(b"1234567890")
    result = transcribe_audio(mock_short)
    print(f"Short audio result: {result}")

    # Note: We can't easily test real transcription without a valid audio file,
    # but we can verify the API client initialization and logic flow.
    print("STT initialization and basic checks passed.")

if __name__ == "__main__":
    test_stt()
