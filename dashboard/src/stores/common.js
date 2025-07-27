import { defineStore } from 'pinia';
import axios from 'axios';

export const useCommonStore = defineStore({
  id: 'common',
  state: () => ({
    // @ts-ignore
    eventSource: null,
    log_cache: [],
    sse_connected: false,

    log_cache_max_len: 1000,
    startTime: -1,

    pluginMarketData: [],
  }),
  actions: {
    async createEventSource() {

      const fetchLogHistory = async () => {
        try {
          const res = await axios.get('/api/log-history');
          if (res.data.data.logs) {
            this.log_cache.push(...res.data.data.logs);
          } else {
            this.log_cache = [];
          }
        } catch (err) {
          console.error('Failed to fetch log history:', err);
        }
      };
      await fetchLogHistory();

      if (this.eventSource) {
        return
      }
      const controller = new AbortController();
      const { signal } = controller;
      const headers = {
        'Content-Type': 'multipart/form-data',
        'Authorization': 'Bearer ' + localStorage.getItem('token')
      };
      fetch('/api/live-log', {
        method: 'GET',
        headers,
        signal,
        cache: 'no-cache',
      }).then(response => {
        if (!response.ok) {
          throw new Error(`SSE connection failed: ${response.status}`);
        }
        console.log('SSE stream opened');
        this.sse_connected = true;

        const reader = response.body.getReader();
        const decoder = new TextDecoder();

        let incompleteLine = ""; // 用于存储不完整的行

        const handleIncompleteLine = (line) => {
          incompleteLine += line;
          // if can parse as JSON, return it
          try {
            const data_json = JSON.parse(incompleteLine);
            incompleteLine = ""; // 清空不完整行
            return data_json;
          } catch (e) {
            return null;
          }
        }

        const processStream = ({ done, value }) => {
          // get bytes length
          const bytesLength = value ? value.byteLength : 0;
          console.log(`Received ${bytesLength} bytes from live log`);
          if (done) {
            console.log('SSE stream closed');
            setTimeout(() => {
              this.eventSource = null;
              this.createEventSource();
            }, 2000);
            return;
          }

          const text = decoder.decode(value);
          const lines = text.split('\n\n');
          lines.forEach(line => {
            if (!line.trim()) {
              return;
            }
            if (line.startsWith('data:')) {
              const data = line.substring(5).trim();
              // {"type":"log","data":"[2021-08-01 00:00:00] INFO: Hello, world!"}
              let data_json = {}
              try {
                data_json = JSON.parse(data);
              } catch (e) {
                console.warn('Invalid JSON:', data);
                // 尝试处理不完整的行
                const parsedData = handleIncompleteLine(data);
                if (parsedData) {
                  data_json = parsedData;
                } else {
                  return; // 如果无法解析，跳过当前行
                }
              }
              if (data_json.type === 'log') {
                this.log_cache.push(data_json);
                if (this.log_cache.length > this.log_cache_max_len) {
                  this.log_cache.shift();
                }
              }
            } else {
              const parsedData = handleIncompleteLine(line);
              if (parsedData && parsedData.type === 'log') {
                this.log_cache.push(parsedData);
                if (this.log_cache.length > this.log_cache_max_len) {
                  this.log_cache.shift();
                }
              }
            }
          });
          return reader.read().then(processStream);
        };

        reader.read().then(processStream);
      }).catch(error => {
        console.error('SSE error:', error);
        // Attempt to reconnect after a delay
        this.log_cache.push('SSE Connection failed, retrying in 5 seconds...');
        setTimeout(() => {
          this.eventSource = null;
          this.createEventSource();
        }, 1000);
      });

      // Store controller to allow closing the connection
      this.eventSource = controller;
    },
    closeEventSourcet() {
      if (this.eventSource) {
        this.eventSource.abort();
        this.eventSource = null;
      }
    },
    getLogCache() {
      return this.log_cache
    },
    getStartTime() {
      if (this.startTime !== -1) {
        return this.startTime
      }
      axios.get('/api/stat/start-time').then((res) => {
        this.startTime = res.data.data.start_time
      })
    },
    async getPluginCollections(force = false) {
      // 获取插件市场数据
      if (!force && this.pluginMarketData.length > 0) {
        return Promise.resolve(this.pluginMarketData);
      }
      return axios.get('/api/plugin/market_list')
        .then((res) => {
          let data = []
          for (let key in res.data.data) {
            data.push({
              "name": key,
              "desc": res.data.data[key].desc,
              "author": res.data.data[key].author,
              "repo": res.data.data[key].repo,
              "installed": false,
              "version": res.data.data[key]?.version ? res.data.data[key].version : "未知",
              "social_link": res.data.data[key]?.social_link,
              "tags": res.data.data[key]?.tags ? res.data.data[key].tags : [],
              "logo": res.data.data[key]?.logo ? res.data.data[key].logo : "",
              "pinned": res.data.data[key]?.pinned ? res.data.data[key].pinned : false,
              "stars": res.data.data[key]?.stars ? res.data.data[key].stars : 0,
              "updated_at": res.data.data[key]?.updated_at ? res.data.data[key].updated_at : "",
            })
          }
          this.pluginMarketData = data;
          return data;
        })
        .catch((err) => {
          return Promise.reject(err);
        });
    },
  }
});
