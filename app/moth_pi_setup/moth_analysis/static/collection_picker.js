(function(){
  function esc(x){
    return String(x == null ? '' : x).replace(/[&<>"']/g,function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function eventCount(row){
    return Number(row.total_events_in_db ?? row.valid_events_in_db ?? row.row_count ?? 0) || 0;
  }
  function rowTime(row){
    return row.collection_end_utc || row.collection_start_utc || row.upload_time_utc || '';
  }
  function rowLabel(row){
    var name = row.collection_name || row.file_name || row.name || 'Collection';
    var meta = [];
    if(row.source_type) meta.push(row.source_type);
    if(row.scan_mode) meta.push(row.scan_mode);
    meta.push(eventCount(row).toLocaleString() + ' events');
    if(rowTime(row)) meta.push(String(rowTime(row)).slice(0,19).replace('T',' '));
    return '#' + row.collection_id + ' ' + name + ' | ' + meta.join(' | ');
  }
  function selectedIds(box){
    return Array.prototype.slice.call(box.querySelectorAll('input[data-collection-id]:checked')).map(function(el){return el.value;});
  }
  function applySelection(cfg, rows){
    var input = document.getElementById(cfg.inputId);
    var box = document.getElementById(cfg.boxId);
    var status = cfg.statusId ? document.getElementById(cfg.statusId) : null;
    if(!input || !box) return;
    var ids = selectedIds(box);
    if(ids.length === 0){
      input.value = '__none__';
      if(status) status.textContent = 'No collections selected. Report will intentionally return no collection evidence.';
    }else{
      input.value = ids.join(',');
      if(status) status.textContent = ids.length + ' of ' + rows.length + ' collections selected.';
    }
    if(typeof cfg.onChange === 'function') cfg.onChange(ids);
  }
  function setAll(cfg, rows, checked){
    var box = document.getElementById(cfg.boxId);
    if(!box) return;
    box.querySelectorAll('input[data-collection-id]').forEach(function(el){el.checked = checked;});
    applySelection(cfg, rows);
  }
  async function init(cfg){
    cfg = cfg || {};
    var input = document.getElementById(cfg.inputId || 'collectionIds');
    var box = document.getElementById(cfg.boxId || 'collectionPicker');
    if(!input || !box) return;
    box.innerHTML = '<div class="collection-picker-status">Loading collections...</div>';
    var rows = [];
    try{
      var r = await fetch('/api/collections', {cache:'no-store'});
      if(!r.ok) throw new Error(await r.text());
      rows = await r.json();
    }catch(e){
      box.innerHTML = '<div class="collection-picker-status bad">Collection list failed: '+esc(e.message || e)+'</div>';
      return;
    }
    if(!rows.length){
      input.value = '__none__';
      box.innerHTML = '<div class="collection-picker-status">No collections loaded.</div>';
      return;
    }
    var typedIds = String(input.value || '').split(/[;,]/).map(function(x){return x.trim();}).filter(Boolean);
    var noneMode = typedIds.indexOf('__none__') >= 0 || typedIds.indexOf('none') >= 0;
    var explicit = typedIds.length && !noneMode;
    var selected = new Set(explicit ? typedIds : rows.map(function(r){return String(r.collection_id);}));
    box.innerHTML =
      '<div class="collection-picker-actions">'+
      '<button type="button" data-picker-action="all">Select all</button>'+
      '<button type="button" data-picker-action="none">Select none</button>'+
      '<button type="button" data-picker-action="reload">Reload list</button>'+
      '<span id="'+esc(cfg.statusId || 'collectionPickerStatus')+'" class="collection-picker-status"></span>'+
      '</div>'+
      '<div class="collection-picker-list">'+rows.map(function(row){
        var id = String(row.collection_id);
        var checked = !noneMode && selected.has(id) ? ' checked' : '';
        var zero = eventCount(row) <= 0 ? ' zero' : '';
        return '<label class="collection-picker-row'+zero+'"><input type="checkbox" data-collection-id value="'+esc(id)+'"'+checked+' /> <span>'+esc(rowLabel(row))+'</span></label>';
      }).join('')+'</div>';
    cfg.statusId = cfg.statusId || 'collectionPickerStatus';
    box.querySelectorAll('input[data-collection-id]').forEach(function(el){el.addEventListener('change',function(){applySelection(cfg, rows);});});
    box.querySelector('[data-picker-action="all"]').addEventListener('click',function(){setAll(cfg, rows, true);});
    box.querySelector('[data-picker-action="none"]').addEventListener('click',function(){setAll(cfg, rows, false);});
    box.querySelector('[data-picker-action="reload"]').addEventListener('click',function(){init(cfg);});
    applySelection(cfg, rows);
  }
  window.LanternCollectionPicker = {init:init};
})();
