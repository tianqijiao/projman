(function () {
  const storageKey = `projman:scroll:${window.location.pathname}${window.location.search}`;

  function restoreScrollPosition() {
    const rawY = window.sessionStorage.getItem(storageKey);
    if (!rawY) {
      return;
    }
    window.sessionStorage.removeItem(storageKey);
    const y = Number(rawY);
    if (!Number.isFinite(y)) {
      return;
    }
    window.requestAnimationFrame(function () {
      window.scrollTo(0, y);
    });
  }

  function bindScrollPreservingForms() {
    document.querySelectorAll("form[data-preserve-scroll]").forEach(function (form) {
      form.addEventListener("submit", function () {
        window.sessionStorage.setItem(storageKey, String(window.scrollY));
      });
    });
  }

  function bindAiDraftForms() {
    document.querySelectorAll("form[data-ai-draft-form]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        const progress = form.querySelector(".ai-progress");
        const submitButton = form.querySelector('button[type="submit"]');
        const message = form.querySelector("[data-ai-progress-message]");
        const percentText = form.querySelector("[data-ai-progress-percent]");
        const progressFill = form.querySelector("[data-ai-progress-fill]");
        const stages = Array.from(form.querySelectorAll("[data-ai-stage]"));
        const stageMessages = [
          "正在上传材料...",
          "正在解析 PDF 和页面图片...",
          "正在调用 AI 识别字段...",
          "正在生成可修改草稿..."
        ];
        const stagePercents = [12, 38, 72, 92];
        let stageIndex = 0;
        let currentPercent = 0;
        let timers = [];

        function setProgress(percent) {
          const nextPercent = Math.max(
            currentPercent,
            Math.min(100, Math.round(percent))
          );
          currentPercent = nextPercent;
          if (progressFill) {
            progressFill.style.width = `${nextPercent}%`;
          }
          if (percentText) {
            percentText.textContent = `${nextPercent}%`;
          }
          if (progress) {
            progress.setAttribute("aria-valuenow", String(nextPercent));
          }
        }

        function setStage(index, percent, customMessage) {
          stageIndex = Math.min(index, stages.length - 1);
          stages.forEach(function (stage, stageItemIndex) {
            stage.classList.toggle("is-active", stageItemIndex === stageIndex);
            stage.classList.toggle("is-done", stageItemIndex < stageIndex);
          });
          if (message) {
            message.textContent = customMessage || stageMessages[stageIndex] || "正在识别材料...";
          }
          setProgress(percent == null ? stagePercents[stageIndex] : percent);
        }

        function clearTimers() {
          timers.forEach(function (timer) {
            window.clearTimeout(timer);
          });
          timers = [];
        }

        function writeResponsePage(html) {
          document.open();
          document.write(html);
          document.close();
        }

        if (form.dataset.aiSubmitting === "true") {
          event.preventDefault();
          return;
        }
        form.dataset.aiSubmitting = "true";
        if (progress) {
          progress.classList.add("is-active");
          progress.setAttribute("aria-hidden", "false");
          progress.setAttribute("aria-valuemin", "0");
          progress.setAttribute("aria-valuemax", "100");
        }
        if (submitButton) {
          submitButton.disabled = true;
          submitButton.textContent = "识别中...";
        }
        setStage(0);

        if (!form.hasAttribute("data-ai-async-form") || !window.XMLHttpRequest || !window.FormData) {
          return;
        }
        event.preventDefault();

        timers.push(window.setTimeout(function () {
          setStage(1);
        }, 800));
        timers.push(window.setTimeout(function () {
          setStage(2);
        }, 2200));
        timers.push(window.setTimeout(function () {
          setStage(3);
        }, 6200));

        function unlockAfterFailure(text) {
          clearTimers();
          form.dataset.aiSubmitting = "false";
          if (message) {
            message.textContent = text;
          }
          if (submitButton) {
            submitButton.disabled = false;
            submitButton.textContent = "重新识别";
          }
        }

        function sendRequest() {
          const xhr = new XMLHttpRequest();
          xhr.open((form.method || "POST").toUpperCase(), form.action);
          xhr.withCredentials = true;

          xhr.upload.onprogress = function (uploadEvent) {
            if (stageIndex > 0) {
              return;
            }
            if (!uploadEvent.lengthComputable || uploadEvent.total <= 0) {
              setStage(0, 20);
              return;
            }
            const uploadRatio = uploadEvent.loaded / uploadEvent.total;
            const uploadPercent = 8 + uploadRatio * 20;
            setStage(
              0,
              uploadPercent,
              `正在上传材料 (${Math.round(uploadRatio * 100)}%)...`
            );
          };

          xhr.onload = function () {
            clearTimers();
            if (xhr.status >= 200 && xhr.status < 400) {
              setStage(3, 100, "识别完成，正在打开草稿...");
              if (xhr.responseURL && xhr.responseURL !== window.location.href) {
                window.setTimeout(function () {
                  window.location.assign(xhr.responseURL);
                }, 120);
                return;
              }
              writeResponsePage(xhr.responseText);
              return;
            }
            writeResponsePage(xhr.responseText);
          };

          xhr.onerror = function () {
            unlockAfterFailure("识别请求中断，请检查网络后重试。");
          };
          xhr.ontimeout = function () {
            unlockAfterFailure("识别等待超时，请稍后重试。");
          };

          xhr.send(new FormData(form));
        }

        window.requestAnimationFrame(function () {
          window.setTimeout(sendRequest, 80);
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    restoreScrollPosition();
    bindScrollPreservingForms();
    bindAiDraftForms();
  });
})();
