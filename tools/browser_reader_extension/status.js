chrome.storage.local.get('reader-status').then(r=>{const s=r['reader-status'];document.getElementById('state').textContent=s?s.text+' · '+new Date(s.at).toLocaleTimeString():'等待连接本机阅读器';});
