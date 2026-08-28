const videoUrl = document.getElementById('videoUrl');
const downloadType = document.getElementById('downloadType');
const downloadButton = document.getElementById('download');
const status = document.getElementById('status');

chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const currentUrl = tabs[0]?.url || '';
  if (currentUrl.includes('youtube.com/') || currentUrl.includes('youtu.be/')) {
    videoUrl.value = currentUrl;
  }
});

downloadButton.addEventListener('click', async () => {
  const url = videoUrl.value.trim();
  if (!url) {
    status.textContent = 'Open a YouTube video or enter its URL.';
    return;
  }

  downloadButton.disabled = true;
  status.textContent = 'Sending cookies and starting download...';

  try {
    const cookies = await chrome.cookies.getAll({ domain: '.youtube.com' });
    const response = await fetch('http://127.0.0.1:200/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, type: downloadType.value, cookies }),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.message || 'Download failed.');
    }
    status.textContent = result.message;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    downloadButton.disabled = false;
  }
});
